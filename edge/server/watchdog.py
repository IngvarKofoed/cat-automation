"""Grab-stall watchdog: restart a wedged edge by exiting for systemd to respawn.

The single grabber thread's camera read has no timeout, so a hung libcamera
``capture_array()`` (or an error-thrash that reopens forever but never succeeds)
freezes ``frame_id``. This watchdog watches the grabber's latest-frame slot and,
when no successful grab has landed for too long, logs and **exits the process** so
the systemd unit (``Restart=always``) respawns it fresh — the only reliable way to
clear process-global libcamera/DMA state, since a hung read holds the source lock
and cannot be interrupted from another thread.

A single "no successful grab for N seconds" rule covers both failure modes (a true
hang and an error-thrash both freeze ``frame_id``). It is evaluated ONLY while the
grabber is running: ``Grabber.stop()`` freezes ``mono``/``frame_id`` by design, so
a stopped grabber must never be read as a stall — this is what keeps the watchdog
from ever ``os._exit``-ing a caller (a test, or shutdown) that merely stopped the
grabber. See docs/specs/2026-07-23-edge-grab-stall-recovery.md.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from edge.server.grabber import FrameSnapshot, Grabber

_log = logging.getLogger("edge.watchdog")

# Exit codes, so `systemctl status` / journalctl name the cause without parsing
# the app log. Distinct from a plain crash (exit 1) or an OOM/segfault (a signal).
EXIT_WEDGE = 70  # camera produced a first frame, then stopped advancing
EXIT_NO_FIRST_FRAME = 71  # camera never produced a first frame within the boot grace

# Defaults (seconds); each env-overridable. WATCHDOG_S = 20 is ~100 healthy frames
# at 5 fps — an unambiguous stall, not a burst of dropped grabs. BOOT_GRACE_S = 60
# out-waits a cold-boot camera warmup before judging "never produced a frame".
_DEFAULT_WATCHDOG_S = 20.0
_DEFAULT_BOOT_GRACE_S = 60.0
_DEFAULT_POLL_S = 2.0
_DEFAULT_HEARTBEAT_S = 30.0


def env_positive_float(name: str, default: float) -> float:
    """Read a positive float from the environment, or fall back to ``default``.

    A missing, non-numeric, or non-positive value yields the default — an
    operator can only *widen* or *narrow* a knob, never disable it by fat-finger.
    """
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _default_on_stall(reason: str, snap: "FrameSnapshot", since: "float | None") -> None:
    """Log ``CRITICAL`` with context, then exit the process for systemd to respawn.

    ``os._exit``, not ``sys.exit``: the watchdog runs in a non-main thread where
    ``SystemExit`` would only unwind that thread, and the grabber thread may be
    stuck in libcamera — the whole wedged process must go, without joining it.
    The ``CRITICAL`` record is flushed first (``StreamHandler`` flushes per record;
    root handlers are flushed here too), so skipping atexit loses nothing — there
    is nothing else to flush (settings write synchronously, no DB on the edge).
    ``since`` is seconds since the last successful grab (None before the first).
    """
    code = EXIT_WEDGE if reason == "wedge" else EXIT_NO_FIRST_FRAME
    last_success = f"{since:.1f}s" if since is not None else "never"
    _log.critical(
        "grab stall (%s): frame_id=%d last_success=%s last_error=%r — exiting %d for systemd restart",
        reason,
        snap.frame_id,
        last_success,
        snap.last_error,
        code,
    )
    # Flush the module logger's own handlers AND root's, so the CRITICAL line
    # survives os._exit whether logging is configured on root (the default via
    # basicConfig, reached by propagation) or with a dedicated edge.watchdog handler.
    for logger in (_log, logging.getLogger()):
        for handler in logger.handlers:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001 - a flush error must not block the exit
                pass
    os._exit(code)


class Watchdog:
    """Polls the grabber's slot and fires ``on_stall`` when a grab stall is detected.

    The stall decision (:meth:`check`) is a pure, testable method; the action
    (``on_stall``) is injected so tests substitute a recorder for the default
    process-exit. Started only when the grabber runs (``create_app`` with
    ``start_grabber=True``); a daemon thread, so it never blocks process exit.
    """

    def __init__(
        self,
        grabber: "Grabber",
        *,
        watchdog_s: "float | None" = None,
        boot_grace_s: "float | None" = None,
        poll_s: float = _DEFAULT_POLL_S,
        heartbeat_s: float = _DEFAULT_HEARTBEAT_S,
        on_stall: "Callable[[str, FrameSnapshot, float | None], None] | None" = None,
    ) -> None:
        self._grabber = grabber
        self._watchdog_s = (
            watchdog_s
            if watchdog_s is not None
            else env_positive_float("CAT_EDGE_WATCHDOG_S", _DEFAULT_WATCHDOG_S)
        )
        self._boot_grace_s = (
            boot_grace_s
            if boot_grace_s is not None
            else env_positive_float("CAT_EDGE_BOOT_GRACE_S", _DEFAULT_BOOT_GRACE_S)
        )
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._on_stall = on_stall or _default_on_stall
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        # Monotonic seconds when the watchdog started — the reference for the
        # boot-grace window (mono is 0 until the first successful grab).
        self._start_mono = 0.0
        self._last_heartbeat_mono = 0.0

    # -- decision (pure) ---------------------------------------------------

    def check(self, now: float) -> "str | None":
        """Return a stall reason (``"wedge"`` / ``"no_first_frame"``) or None.

        Never a stall while the grabber isn't running: ``stop()`` freezes the slot
        by design. Armed once a first frame exists — stalled when the last success
        is older than ``watchdog_s``. Before any frame — stalled once the boot
        grace elapses (a camera that never came up).
        """
        if not self._grabber.is_running():
            return None
        return self._classify(self._grabber.snapshot(), now)

    def _classify(self, snap: "FrameSnapshot", now: float) -> "str | None":
        """Stall reason for one snapshot (pure). frame_id 0 → the boot-grace path."""
        if snap.frame_id > 0:
            return "wedge" if now - snap.mono > self._watchdog_s else None
        return "no_first_frame" if now - self._start_mono > self._boot_grace_s else None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the daemon poll loop. Idempotent — a second call is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._start_mono = time.monotonic()
        self._last_heartbeat_mono = self._start_mono
        self._thread = threading.Thread(
            target=self._run, name="edge-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to exit and wait briefly. Safe when not running."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _run(self) -> None:
        # stop.wait doubles as the pace: returns True at once when stopped, else
        # False after poll_s. First check is one interval in (out-waits warmup).
        while not self._stop.wait(self._poll_s):
            now = time.monotonic()
            if not self._grabber.is_running():
                continue  # stopped grabber (shutdown/test): never a stall, no heartbeat
            snap = self._grabber.snapshot()
            reason = self._classify(snap, now)
            if reason is not None:
                since = (now - snap.mono) if snap.frame_id > 0 else None
                self._on_stall(reason, snap, since)
                return  # default on_stall exits; an injected no-op ends the loop
            self._maybe_heartbeat(now, snap)

    def _maybe_heartbeat(self, now: float, snap: "FrameSnapshot") -> None:
        if now - self._last_heartbeat_mono < self._heartbeat_s:
            return
        self._last_heartbeat_mono = now
        last_success = f"{now - snap.mono:.1f}s ago" if snap.frame_id > 0 else "never"
        _log.info(
            "edge alive: frame_id=%d last_success=%s motion=%s last_error=%r",
            snap.frame_id,
            last_success,
            snap.motion,
            snap.last_error,
        )

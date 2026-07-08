"""Background frame grabber and latest-frame slot for the edge tier.

One daemon thread reads the current capture source at the configured fps and
publishes each decoded frame into a single slot (``frame``, monotonic
``frame_id``, ``ts``, ``last_error``) guarded by a ``threading.Condition``. Both
``/stream`` and ``/frame`` serve from this slot, so the camera read-rate is
decoupled from the client count and fps pacing lives in one place.

The slot holds the RAW, untransformed BGR frame; each consumer applies
rotate/crop at the serving boundary, so rotation/ROI changes appear live without
restarting the thread.

See docs/ARCHITECTURE.md (The Pi as a thin smart-camera node; Communication and
data flow) and docs/specs/2026-07-07-edge-stream-live-fps.md.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, NamedTuple

if TYPE_CHECKING:  # only for annotations — keep runtime imports light
    import numpy as np

    from edge.capture.base import CaptureSource

# Pacing fallback used only in the degenerate case where read_config can't be
# reached or hands back a non-positive fps; it just keeps the loop from busy-
# spinning or dividing by zero. It is NOT the config default (that lives in
# settings.py) — it never overrides a valid configured fps.
_DEFAULT_FPS = 5.0


class FrameSnapshot(NamedTuple):
    """An immutable, atomic view of the grabber's latest-frame slot.

    Fields:
        frame:      raw untransformed BGR ndarray, or None before the first
                    successful grab.
        frame_id:   0 before the first success; +1 on each successful grab;
                    never advances on a failed grab.
        ts:         epoch milliseconds of the successful grab (0 before any).
                    Wall-clock, for the X-Timestamp wire header only.
        last_error: str set on the most recent failed grab, or None after a
                    success (or before the first grab).
        mono:       time.monotonic() seconds of the successful grab (0.0 before
                    any). Immune to clock steps, so freshness/staleness deltas
                    are computed from this, never from the wall-clock ts (the Pi
                    has no RTC and NTP steps the clock forward after boot).
    """

    frame: "np.ndarray | None"
    frame_id: int
    ts: int
    last_error: "str | None"
    mono: float


class Grabber:
    """Owns the single camera read loop and publishes into the latest-frame slot.

    ``read_config`` is called once per iteration to obtain ``(source, fps)``. The
    app snapshots those under ITS own config lock inside ``read_config``; the
    grabber then calls ``source.read()`` WITHOUT holding any app lock, so grabs
    never block config reads or a device swap. Because reads are lock-free, a
    closed (poisoned) source may be read once during a swap and raise — that is
    handled like any other failed grab and self-heals on the next iteration.
    """

    def __init__(self, read_config: "Callable[[], tuple[CaptureSource, float]]") -> None:
        self._read_config = read_config

        # The slot and its guard. Kept separate from the app's config lock so
        # waiting for a frame never holds the config lock.
        self._cond = threading.Condition()
        self._frame: "np.ndarray | None" = None
        self._frame_id = 0
        self._ts = 0
        self._mono = 0.0
        self._last_error: "str | None" = None

        # Pacing fallback; owned by the grab path.
        self._fps_fallback = _DEFAULT_FPS

        # Thread lifecycle.
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._lifecycle_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the daemon grab loop. Idempotent — a second call is a no-op."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="edge-grabber", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Signal the loop to exit and wait briefly for it to wind down.

        Safe to call when not running. The pacing sleep is interruptible, so the
        loop exits promptly unless a ``read()`` is hung (a known gap deferred to
        the motion increment's watchdog).
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    # -- grabbing ----------------------------------------------------------

    def grab_once(self) -> None:
        """Perform EXACTLY ONE grab iteration (read_config -> read -> publish).

        No sleep and no thread — safe to call before/without ``start()`` for
        deterministic tests.
        """
        self._grab_once_internal()

    def _grab_once_internal(self) -> float:
        """Run one grab iteration and return the fps to pace the running loop by.

        The whole body is wrapped so ANY exception (``CaptureError`` or
        otherwise) is handled like a failed grab — ``last_error`` is stored,
        ``frame_id`` is left unchanged (so streams don't re-emit a stale frame),
        and the sole producer thread can never be killed. ``notify_all()`` fires
        on every grab, success or failure.
        """
        fps = self._fps_fallback
        try:
            source, cfg_fps = self._read_config()
            if (
                isinstance(cfg_fps, (int, float))
                and not isinstance(cfg_fps, bool)
                and cfg_fps > 0
            ):
                fps = float(cfg_fps)
                self._fps_fallback = fps
            img = source.read()
            ts = int(time.time() * 1000)
            mono = time.monotonic()
            with self._cond:
                self._frame = img
                self._ts = ts
                self._mono = mono
                self._last_error = None
                self._frame_id += 1  # monotonic; advances only on success
                self._cond.notify_all()
        except Exception as exc:  # noqa: BLE001 - a failed grab must never kill the loop
            with self._cond:
                self._last_error = str(exc) or repr(exc)
                self._cond.notify_all()
        return fps

    def _run(self) -> None:
        """The background loop: grab, then pace to the configured fps."""
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                fps = self._grab_once_internal()
            except Exception:  # noqa: BLE001 - defensive; the loop must never die
                fps = self._fps_fallback
            elapsed = time.monotonic() - start
            # Interruptible pace: returns at once when stop is set (or delay 0).
            self._stop.wait(max(0.0, (1.0 / fps) - elapsed))

    # -- reading the slot --------------------------------------------------

    def snapshot(self) -> FrameSnapshot:
        """Return the current slot atomically, without waiting."""
        with self._cond:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> FrameSnapshot:
        """Build a snapshot; caller must hold ``self._cond``."""
        return FrameSnapshot(
            self._frame, self._frame_id, self._ts, self._last_error, self._mono
        )

    def wait_first(self, timeout: "float | None") -> FrameSnapshot:
        """Block until the first frame arrives, an error is reported, or timeout.

        Returns as soon as ``frame_id > 0`` OR ``last_error`` is set OR
        ``timeout`` (seconds) elapses. Used by ``/frame`` to out-wait camera
        warmup on boot.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._frame_id == 0 and self._last_error is None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._snapshot_locked()

    def wait_next(self, after_id: int, timeout: "float | None") -> FrameSnapshot:
        """Block until a frame newer than ``after_id`` arrives, or timeout.

        Returns as soon as ``frame_id > after_id`` OR ``timeout`` (seconds)
        elapses. Drives ``/stream``'s per-frame cadence off the grabber so idle
        clients don't busy-loop.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._frame_id <= after_id:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._snapshot_locked()

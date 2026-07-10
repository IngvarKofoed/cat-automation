"""Background frame grabber, latest-frame slot, and MOG2 motion detection.

One daemon thread reads the current capture source at the configured fps and
publishes each decoded frame into a single slot (``frame``, monotonic
``frame_id``, ``ts``, ``last_error``, plus the motion decision) guarded by a
``threading.Condition``. Both ``/stream`` and ``/frame`` serve from this slot,
so the camera read-rate is decoupled from the client count and fps pacing lives
in one place.

The slot holds the RAW, untransformed BGR frame; each consumer applies
rotate/crop at the serving boundary, so rotation/ROI changes appear live without
restarting the thread.

Motion detection runs in this same loop (one producer, no extra thread): after
each successful read the grabber runs MOG2 background subtraction over the
rotate+crop-derived, downscaled grayscale ROI and publishes ``motion``/``bbox``/
``area`` into the slot alongside the frame. Motion is a PULL signal only — it is
never used to gate frame delivery; ``/stream`` and ``/frame`` keep serving every
frame. See docs/ARCHITECTURE.md (The Pi as a thin smart-camera node;
Communication and data flow), docs/specs/2026-07-07-edge-stream-live-fps.md, and
docs/specs/2026-07-08-edge-motion-detection.md.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, NamedTuple

from edge.clip.transform import crop, rotate
from shared.motion import MotionGate, MotionParams

if TYPE_CHECKING:  # only for annotations — keep runtime imports light
    import numpy as np

    from edge.capture.base import CaptureSource

# Pacing fallback used only in the degenerate case where read_config can't be
# reached or hands back a non-positive fps; it just keeps the loop from busy-
# spinning or dividing by zero. It is NOT the config default (that lives in
# settings.py) — it never overrides a valid configured fps.
_DEFAULT_FPS = 5.0

# The motion params (var_threshold/learning_rate/min_area/max_area_fraction/
# persistence/downscale) now live in shared.motion, imported by both tiers so
# the offline tuning re-run matches the live gate by construction. Re-exported
# under the historical name so app.py and existing imports are unaffected.
MotionConfig = MotionParams


class GrabConfig(NamedTuple):
    """One iteration's config, snapshotted by the app under ITS config lock.

    The grabber reads ``source.read()`` WITHOUT holding the app lock, so grabs
    never block config reads or a device swap; everything the iteration needs
    (source, pacing, and the transform+motion params) is captured here first.
    """

    source: "CaptureSource"
    fps: float
    rotation: int
    clip: "dict | None"
    motion: "MotionConfig"


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
        motion:     debounced motion decision for the published frame; False
                    before the first decision.
        bbox:       (x, y, w, h) of the largest foreground blob normalized to
                    the ROI (0..1) when ``motion`` is active, else None.
        area:       largest foreground blob's area as a fraction of the ROI,
                    always reported for tuning (0.0 when there is no blob).
    """

    frame: "np.ndarray | None"
    frame_id: int
    ts: int
    last_error: "str | None"
    mono: float
    motion: bool
    bbox: "tuple | None"
    area: float


class Grabber:
    """Owns the single camera read loop and publishes into the latest-frame slot.

    ``read_config`` is called once per iteration to obtain a ``GrabConfig``
    (source, fps, rotation, clip, and the motion params). The app snapshots
    those under ITS own config lock inside ``read_config``; the grabber then
    calls ``source.read()`` WITHOUT holding any app lock, so grabs never block
    config reads or a device swap. Because reads are lock-free, a closed
    (poisoned) source may be read once during a swap and raise — that is handled
    like any other failed grab and self-heals on the next iteration.

    Each successful read is followed by the motion step (MOG2 over the
    downscaled grayscale ROI); its result rides the same slot as the frame.
    """

    def __init__(self, read_config: "Callable[[], GrabConfig]") -> None:
        self._read_config = read_config

        # The slot and its guard. Kept separate from the app's config lock so
        # waiting for a frame never holds the config lock.
        self._cond = threading.Condition()
        self._frame: "np.ndarray | None" = None
        self._frame_id = 0
        self._ts = 0
        self._mono = 0.0
        self._last_error: "str | None" = None
        self._motion = False
        self._bbox: "tuple | None" = None
        self._area = 0.0

        # The shared MOG2 motion gate (learned background + debounce streak). It
        # is not internally locked, so the grab thread (which calls process) and
        # reset_motion() callers (config changes / manual relearn) serialize on
        # _motion_lock — the gate's learned background is its whole value; only a
        # reset drops it.
        self._motion_lock = threading.Lock()
        self._gate = MotionGate()

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
        """Perform EXACTLY ONE grab iteration (read_config -> read -> motion -> publish).

        No sleep and no thread — safe to call before/without ``start()`` for
        deterministic tests. The motion step runs as part of the iteration.
        """
        self._grab_once_internal()

    def _grab_once_internal(self) -> float:
        """Run one grab iteration and return the fps to pace the running loop by.

        Two-stage so that MOTION NEVER GATES DELIVERY (the core contract): a
        failed read_config/read is a failed grab (``last_error`` set, ``frame_id``
        and the motion fields left unchanged, so streams don't re-emit a stale
        frame); but once a read SUCCEEDS the frame is always published, and the
        motion step runs in its OWN guard so a motion-compute error (e.g. a
        codec/format quirk on an odd camera) degrades to "no motion" for that
        frame instead of suppressing an otherwise-good one. Either way the sole
        producer thread can never be killed; ``notify_all()`` fires on every grab.
        """
        fps = self._fps_fallback
        try:
            cfg = self._read_config()
            cfg_fps = cfg.fps
            if (
                isinstance(cfg_fps, (int, float))
                and not isinstance(cfg_fps, bool)
                and cfg_fps > 0
            ):
                fps = float(cfg_fps)
                self._fps_fallback = fps
            img = cfg.source.read()
        except Exception as exc:  # noqa: BLE001 - a failed grab must never kill the loop
            with self._cond:
                self._last_error = str(exc) or repr(exc)
                self._cond.notify_all()
            return fps

        # The read succeeded, so the frame WILL be published regardless of motion.
        # Motion is a pull signal, never on the delivery path (see the spec), so a
        # motion-compute failure must not suppress the frame — it degrades to
        # "no motion" for this frame and self-heals on the next.
        ts = int(time.time() * 1000)
        mono = time.monotonic()
        try:
            motion, bbox, area = self._compute_motion(
                img, cfg.rotation, cfg.clip, cfg.motion
            )
        except Exception:  # noqa: BLE001 - motion must never gate delivery or kill the loop
            motion, bbox, area = False, None, 0.0
        with self._cond:
            self._frame = img
            self._ts = ts
            self._mono = mono
            self._last_error = None
            self._motion = motion
            self._bbox = bbox
            self._area = area
            self._frame_id += 1  # monotonic; advances only on a successful read
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

    # -- motion ------------------------------------------------------------

    def _compute_motion(
        self, frame, rotation, clip, cfg: MotionConfig
    ) -> "tuple[bool, tuple | None, float]":
        """Compute the debounced motion decision for one raw frame.

        Applies this edge's rotate+crop (so motion tracks exactly the door
        region the serving routes show), then hands the ROI to the shared
        ``MotionGate`` — the downscale → gray → MOG2 → threshold → morph →
        largest-blob → locality/persistence core lives there, identical to the
        compute tier's offline re-run. Returns ``(motion, bbox, area)`` where
        ``area`` is the largest foreground blob as a fraction of the ROI (always
        reported, for tuning) and ``bbox`` is that blob normalized to the ROI
        (0..1) when motion is active, else None.

        The gate is not internally locked, so ``process`` runs under
        ``self._motion_lock`` — a concurrent ``reset_motion()`` can't swap the
        model or the debounce streak mid-computation. Any OpenCV/transform error
        propagates to the caller, which treats it as a failed grab.
        """
        # Transform is stateless and touches no shared state — do it outside the
        # lock so _motion_lock is held only around the gate's MOG2 + post-processing.
        roi = crop(rotate(frame, rotation), clip)
        with self._motion_lock:
            return self._gate.process(roi, cfg)

    def reset_motion(self) -> None:
        """Drop the MOG2 model and zero the debounce counter (relearn next grab).

        Thread-safe. The model is tied to the exact ROI pixels/dimensions, so
        anything that changes its input imagery — a device swap, a clip/rotation
        change, or the UI's manual relearn — must recreate it, else new pixels
        would be compared against a stale model and burst false motion. The next
        grab recreates the instance lazily.

        Also clears the PUBLISHED motion/bbox/area to neutral: after a
        clip/rotation change the old bbox is normalized to the PREVIOUS ROI
        geometry, so serving it (via /status, or the /frame?overlay box) until the
        next grab would report motion at a location that no longer matches the
        current frame. Cleared here so no stale motion signal survives a reset.
        """
        with self._motion_lock:
            self._gate.reset()
        # Separate acquire (never nested with _motion_lock — matches the grab
        # path, which holds only one of the two at a time), so no lock-order risk.
        with self._cond:
            self._motion = False
            self._bbox = None
            self._area = 0.0
            self._cond.notify_all()

    # -- reading the slot --------------------------------------------------

    def snapshot(self) -> FrameSnapshot:
        """Return the current slot atomically, without waiting."""
        with self._cond:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> FrameSnapshot:
        """Build a snapshot; caller must hold ``self._cond``."""
        return FrameSnapshot(
            self._frame,
            self._frame_id,
            self._ts,
            self._last_error,
            self._mono,
            self._motion,
            self._bbox,
            self._area,
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

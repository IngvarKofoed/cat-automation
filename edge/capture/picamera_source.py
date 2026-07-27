"""Picamera2 capture backend for the Raspberry Pi CSI camera.

Drives the Pi Camera Module through libcamera/Picamera2 — the CSI path OpenCV's
V4L2 backend cannot capture from on current Raspberry Pi OS. Picamera2 is a
Pi-only, apt-installed package (`python3-picamera2`); it is imported lazily so
this module still imports on non-Pi machines, where `read()` simply fails with
CaptureError. See docs/ARCHITECTURE.md (Camera source).
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from edge.capture.base import CaptureError, CaptureSource

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# Frames to let auto WB converge on before reading back its gains in
# ``awb_lock_once``. ``capture_metadata()`` blocks for the next frame, so this is
# a frame count, not a sleep — at the edge's ~5 fps it is ~2 s. AWB settles in
# well under that from a cold start; the cost is paid once, on an operator click.
_AWB_SETTLE_FRAMES = 10


class PicameraCaptureSource(CaptureSource):
    """A CaptureSource backed by Picamera2 (a Pi CSI camera).

    Same contract as the OpenCV backend: opens lazily on first read, keeps the
    camera running between reads, and self-heals — any failed read tears the
    camera down so the next read reopens from scratch.
    """

    def __init__(self, index: int = 0) -> None:
        self._index = index
        self._cam = None
        self._lock = threading.Lock()
        self._closed = False
        # Desired focus: None = continuous autofocus, a float = manual lens
        # position (dioptres). Held here so it survives a self-heal reopen and
        # so set_focus() before the first read() still takes effect on open.
        self._focus: "float | None" = None
        # Desired white balance: None = continuous auto, (red, blue) = locked at
        # those gains. Same survives-a-reopen reasoning as focus.
        self._awb_gains: "tuple[float, float] | None" = None
        # libcamera tuning file name, or None for the backend default. Unlike
        # focus/AWB this is a CONSTRUCTION-time choice, so changing it tears the
        # camera down rather than pushing a control (see set_tuning_file).
        self._tuning_file: "str | None" = None

    def read(self) -> "np.ndarray":
        with self._lock:
            if self._closed:
                raise CaptureError(f"picamera2 source {self._index} is closed")
            try:
                self._ensure_open()
                return self._cam.capture_array()  # HxWx3 uint8, BGR (see below)
            except Exception as e:  # noqa: BLE001 - any failure invalidates the camera
                self._close_locked()
                raise CaptureError(f"picamera2 capture failed: {e}") from e

    def close(self) -> None:
        """Release the camera. Safe to call more than once.

        Poisons the source: a subsequent read() raises CaptureError instead of
        reopening. A transient read failure calls _close_locked() (not close()),
        leaving _closed False so it still self-heals — only an explicit close()
        poisons, sealing the device-swap race for the CSI backend too.
        """
        with self._lock:
            self._closed = True
            self._close_locked()

    # -- focus control -----------------------------------------------------

    def focus_capabilities(self) -> "dict | None":
        """Report the lens dioptre range if this camera has a movable lens.

        Opens the camera if needed to read ``camera_controls``; returns None
        when picamera2 is absent (not a Pi), the camera can't open, or it has no
        ``LensPosition`` control (a fixed-focus Module 1/2). Never raises — a
        focus-capability probe must not take the config UI down.
        """
        with self._lock:
            if self._closed:
                return None
            try:
                self._ensure_open()
                controls = self._cam.camera_controls
            except Exception:  # noqa: BLE001 - not a Pi / libcamera error → no focus UI
                return None
        lens = controls.get("LensPosition") if isinstance(controls, dict) else None
        if not lens:  # absent, or a fixed-focus camera reporting an empty/None range
            return None
        try:
            lo, hi, _default = lens
            return {"min": float(lo), "max": float(hi)}
        except (TypeError, ValueError):
            return None

    def set_focus(self, focus: "float | None") -> None:
        """Remember the desired focus and apply it live if the camera is open."""
        with self._lock:
            self._focus = focus
            self._apply_focus_locked()

    def autofocus_once(self) -> "float | None":
        """Run one AF cycle, lock manual focus at the result, and return it.

        Returns the found lens position (dioptres), or None if the camera has no
        autofocus. Raises CaptureError if an AF-capable camera's cycle fails.
        Holds the source lock for the whole (~sub-second) cycle, so a concurrent
        grabber read simply waits — no two libcamera calls overlap.
        """
        with self._lock:
            if self._closed:
                raise CaptureError(f"picamera2 source {self._index} is closed")
            try:
                self._ensure_open()
                from libcamera import controls as libcontrols  # lazy: Pi-only

                if "LensPosition" not in self._cam.camera_controls:
                    return None  # fixed-focus camera (Module 1/2)
                self._cam.set_controls({"AfMode": libcontrols.AfModeEnum.Auto})
                converged = self._cam.autofocus_cycle()  # blocks until AF settles
                lens = self._cam.capture_metadata().get("LensPosition")
            except Exception as e:  # noqa: BLE001 - surface as CaptureError, don't crash
                raise CaptureError(f"picamera2 autofocus failed: {e}") from e
            if not converged or lens is None:
                # AF-capable camera that couldn't settle (low-contrast/dark door
                # scene). Restore the prior desired focus, then surface a DISTINCT,
                # retryable error rather than None — None means "no focus hardware"
                # to the caller and would map to a permanent 422, misreporting a
                # transient miss as an unfocusable camera.
                self._apply_focus_locked()
                raise CaptureError(
                    "autofocus did not converge — improve lighting/contrast, "
                    "or set focus manually"
                )
            self._focus = float(lens)
            self._apply_focus_locked()  # lock manual focus at the found position
            return self._focus

    def _apply_focus_locked(self) -> None:
        """Push ``self._focus`` to the open camera. Caller holds the lock.

        None → continuous autofocus; a number → manual lens locked at that
        dioptre value. No-op when the camera is closed/absent or fixed-focus.
        Best-effort: focus is never on the delivery path, so any libcamera error
        is swallowed rather than allowed to break capture.
        """
        if self._cam is None:
            return
        try:
            from libcamera import controls as libcontrols  # lazy: Pi-only

            if "LensPosition" not in self._cam.camera_controls:
                return  # fixed-focus camera: nothing to set
            if self._focus is None:
                self._cam.set_controls({"AfMode": libcontrols.AfModeEnum.Continuous})
            else:
                self._cam.set_controls({
                    "AfMode": libcontrols.AfModeEnum.Manual,
                    "LensPosition": float(self._focus),
                })
        except Exception:  # noqa: BLE001 - focus is best-effort, never gate capture
            pass

    # -- white-balance control ---------------------------------------------

    def awb_capabilities(self) -> "dict | None":
        """Report the colour-gain range if this camera's white balance is settable.

        Mirrors ``focus_capabilities``: opens the camera if needed, returns None
        when picamera2 is absent, the camera can't open, or there is no
        ``ColourGains`` control. Never raises — a capability probe must not take
        the config UI down.
        """
        with self._lock:
            if self._closed:
                return None
            try:
                self._ensure_open()
                controls = self._cam.camera_controls
            except Exception:  # noqa: BLE001 - not a Pi / libcamera error → no AWB UI
                return None
        gains = controls.get("ColourGains") if isinstance(controls, dict) else None
        if not gains:
            return None
        try:
            lo, hi, _default = gains
            return {"min": float(lo), "max": float(hi)}
        except (TypeError, ValueError):
            return None

    def set_awb(self, gains: "tuple[float, float] | None") -> None:
        """Remember the desired white balance and apply it live if the camera is open."""
        with self._lock:
            self._awb_gains = None if gains is None else (float(gains[0]), float(gains[1]))
            self._apply_awb_locked()

    def awb_lock_once(self) -> "tuple[float, float] | None":
        """Let auto WB converge, then lock and return the gains it settled on.

        Returns ``(red, blue)``, or None if the camera has no colour-gain control.
        Raises CaptureError if an AWB-capable camera's cycle fails. Holds the
        source lock for the whole settle (~``_AWB_SETTLE_FRAMES`` frames), so a
        concurrent grabber read waits rather than interleaving libcamera calls —
        the same posture ``autofocus_once`` takes.
        """
        with self._lock:
            if self._closed:
                raise CaptureError(f"picamera2 source {self._index} is closed")
            try:
                self._ensure_open()
                if "ColourGains" not in self._cam.camera_controls:
                    return None  # no settable white balance on this camera
                # Hand control back to AWB and let it converge. capture_metadata()
                # blocks for the next frame, so the loop IS the settle time.
                self._cam.set_controls({"AwbEnable": True})
                gains = None
                for _ in range(_AWB_SETTLE_FRAMES):
                    gains = self._cam.capture_metadata().get("ColourGains")
            except Exception as e:  # noqa: BLE001 - surface as CaptureError, don't crash
                raise CaptureError(f"picamera2 AWB lock failed: {e}") from e
            if not gains:
                # AWB-capable camera that reported no gains. Restore the prior
                # desired setting, then raise a DISTINCT retryable error — None
                # means "no AWB hardware" to the caller and would map to a
                # permanent 422, misreporting a transient miss as an incapable
                # camera. Exactly the distinction autofocus_once draws.
                self._apply_awb_locked()
                raise CaptureError(
                    "auto white balance reported no gains — retry with the scene lit"
                )
            self._awb_gains = (float(gains[0]), float(gains[1]))
            self._apply_awb_locked()  # lock at what it converged on
            return self._awb_gains

    def _apply_awb_locked(self) -> None:
        """Push ``self._awb_gains`` to the open camera. Caller holds the lock.

        None → continuous auto white balance; a pair → AWB off, gains locked
        there. No-op when the camera is closed/absent or has no gain control.
        Best-effort for the same reason focus is: white balance is never on the
        delivery path, so a libcamera error must not break capture.
        """
        if self._cam is None:
            return
        try:
            if "ColourGains" not in self._cam.camera_controls:
                return
            if self._awb_gains is None:
                self._cam.set_controls({"AwbEnable": True})
            else:
                self._cam.set_controls({
                    "AwbEnable": False,
                    "ColourGains": (float(self._awb_gains[0]), float(self._awb_gains[1])),
                })
        except Exception:  # noqa: BLE001 - best-effort, never gate capture
            pass

    # -- sensor tuning file -------------------------------------------------

    def set_tuning_file(self, name: "str | None") -> None:
        """Select the libcamera tuning file, rebuilding the camera if it changed.

        Unlike focus and AWB this is a Picamera2 CONSTRUCTOR argument, so it can
        only take effect on a fresh camera object. Tearing down here (not
        ``close()`` — that would poison the source) lets the next ``read()``
        reopen with the new tuning, reusing the self-heal path that already
        exists rather than adding a second reopen mechanism.
        """
        with self._lock:
            if name == self._tuning_file:
                return
            self._tuning_file = name
            self._close_locked()

    def _ensure_open(self) -> None:
        if self._cam is not None:
            return
        from picamera2 import Picamera2  # lazy: Pi-only dependency

        tuning = None
        if self._tuning_file:
            try:
                tuning = Picamera2.load_tuning_file(self._tuning_file)
            except Exception as e:  # noqa: BLE001 - a bad name must not stop capture
                # Fail SOFT to the default tuning: a typo'd or missing tuning file
                # is a colour-accuracy problem, and bricking the camera over it
                # would take the whole door offline. Logged (not swallowed) so the
                # journal says why the colours look wrong — the same visibility
                # posture the grabber's failure logging takes.
                logger.warning(
                    "tuning file %r could not be loaded (%s); using the default tuning",
                    self._tuning_file, e,
                )
        cam = Picamera2(self._index) if tuning is None else Picamera2(self._index, tuning=tuning)
        # A VIDEO configuration for the continuous grab loop — NOT a still one.
        # create_still_configuration allocates a SINGLE full-resolution buffer
        # (stills are one-shot), so reading it in a ~5 fps loop starves the
        # pipeline and libcamera hands back half-filled buffers: the green
        # stripes / purple frames seen on the Module 3. A video config with
        # several buffers keeps whole frames flowing.
        #   * buffer_count=4 — enough queue depth that a grab never races the
        #     sensor's DMA into the buffer it's handing us.
        #   * size 2304x1296 — the IMX708's 2x2-binned full-FoV mode: far less
        #     than the 4608x2592 full-res still default (which a ~640x480 door
        #     ROI never needs), lighter on the Pi, and lower-noise at night.
        #     Any CSI camera without this exact mode gets the nearest one
        #     ISP-scaled; the clip is normalized, so resolution is transparent
        #     downstream. It may also quiet the "PDAF data in unsupported
        #     format" log spam, which rides the full-res mode.
        # Picamera2's format naming is inverted vs. the numpy byte order: asking
        # for "RGB888" yields a BGR-ordered array, which is exactly what cv2 and
        # the CaptureSource contract expect. If the preview colors look swapped
        # on your Pi, switch this one string to "BGR888".
        cam.configure(cam.create_video_configuration(
            main={"size": (2304, 1296), "format": "RGB888"},
            buffer_count=4,
        ))
        cam.start()
        self._cam = cam
        # Apply the desired focus now that the camera is running (the default
        # None selects continuous AF — better than the sensor's power-on lens
        # position, which sits near infinity and blurs a cat at the flap).
        self._apply_focus_locked()
        # Same for white balance, so a locked pair survives a self-heal reopen —
        # otherwise a transient read failure would silently hand the scene back
        # to auto WB and move every downstream colour reading.
        self._apply_awb_locked()

    def _close_locked(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._cam = None

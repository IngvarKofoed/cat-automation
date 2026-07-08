"""Picamera2 capture backend for the Raspberry Pi CSI camera.

Drives the Pi Camera Module through libcamera/Picamera2 — the CSI path OpenCV's
V4L2 backend cannot capture from on current Raspberry Pi OS. Picamera2 is a
Pi-only, apt-installed package (`python3-picamera2`); it is imported lazily so
this module still imports on non-Pi machines, where `read()` simply fails with
CaptureError. See docs/ARCHITECTURE.md (Camera source).
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from edge.capture.base import CaptureError, CaptureSource

if TYPE_CHECKING:
    import numpy as np


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

    def _ensure_open(self) -> None:
        if self._cam is not None:
            return
        from picamera2 import Picamera2  # lazy: Pi-only dependency

        cam = Picamera2(self._index)
        # Picamera2's format naming is inverted vs. the numpy byte order: asking
        # for "RGB888" yields a BGR-ordered array, which is exactly what cv2 and
        # the CaptureSource contract expect. If the preview colors look swapped
        # on your Pi, switch this one string to "BGR888".
        cam.configure(cam.create_still_configuration(main={"format": "RGB888"}))
        cam.start()
        self._cam = cam

    def _close_locked(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._cam = None

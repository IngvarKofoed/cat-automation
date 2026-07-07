"""OpenCV capture backend for the edge tier.

A single `cv2.VideoCapture` backend that runs on both a Mac (AVFoundation) and a
Raspberry Pi (V4L2) for development and deployment. See docs/ARCHITECTURE.md
(Camera source) and docs/specs/2026-07-07-edge-stills-mvp.md.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import cv2

from .base import CaptureError, CaptureSource

if TYPE_CHECKING:
    import numpy as np


class OpenCVCaptureSource(CaptureSource):
    """A capture source backed by a persistent `cv2.VideoCapture` handle.

    The handle is opened lazily on first `read()` and kept open between calls
    (OpenCV capture warmup is slow). All access is guarded by a lock because
    OpenCV capture is not thread-safe. On any failed read the handle is released
    and invalidated so the next `read()` reopens from scratch (self-healing);
    recovery is retry-on-next-read, with no timed backoff.
    """

    def __init__(self, device: int | str = 0) -> None:
        # `device` is an opaque camera id: an int index (macOS/USB) or a
        # device-path string like "/dev/video0" (Linux). cv2 accepts both.
        self._device = device
        self._lock = threading.Lock()
        self._cap: "cv2.VideoCapture | None" = None

    def read(self) -> "np.ndarray":
        """Return one decoded BGR frame, reopening the handle if needed."""
        with self._lock:
            if self._cap is None:
                self._cap = cv2.VideoCapture(self._device)
            if not self._cap.isOpened():
                self._release_locked()
                raise CaptureError(f"cannot open camera {self._device!r}")

            ok, frame = self._cap.read()
            if not ok or frame is None:
                self._release_locked()
                raise CaptureError(f"failed to read frame from camera {self._device!r}")

            return frame

    def close(self) -> None:
        """Release the underlying handle. Safe to call more than once."""
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        """Release and drop the handle. Caller must hold the lock."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None

"""Default capture-source factory: pick a backend from the opaque device id.

The device id (see docs/specs/2026-07-07-edge-stills-mvp.md) is opaque. An int
index or a "/dev/video*" path routes to the OpenCV backend; a "csi" / "csi:N" id
routes to the Picamera2 (CSI) backend. Tests inject their own factory (e.g.
FakeCaptureSource) instead of this one.
"""
from __future__ import annotations

from edge.capture.base import CaptureSource
from edge.capture.opencv_source import OpenCVCaptureSource


def create_source(device: "int | str") -> CaptureSource:
    """Build the CaptureSource for an opaque device id."""
    if isinstance(device, str) and (device == "csi" or device.startswith("csi:")):
        # "csi" or "csi:N" -> Pi CSI camera N (default 0). Imported here so the
        # Pi-only Picamera2 dependency is never pulled in on other platforms.
        from edge.capture.picamera_source import PicameraCaptureSource

        _, _, tail = device.partition(":")
        return PicameraCaptureSource(int(tail) if tail.isdigit() else 0)
    return OpenCVCaptureSource(device)

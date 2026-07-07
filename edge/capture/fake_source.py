"""Fake capture source used by tests — no camera hardware required.

See docs/ARCHITECTURE.md (Camera source) and
docs/specs/2026-07-07-edge-stills-mvp.md.
"""
from __future__ import annotations

import numpy as np

from .base import CaptureSource


class FakeCaptureSource(CaptureSource):
    """Returns a synthetic BGR frame instead of reading a real camera.

    Accepts (and ignores) a ``device`` argument so it can be passed directly
    as a ``source_factory`` wherever a real backend's constructor would be.
    """

    def __init__(self, device: int | str = 0) -> None:
        del device  # unused: kept only for interface compatibility

    def read(self) -> np.ndarray:
        """Return a small synthetic (240, 320, 3) uint8 BGR gradient frame."""
        row = np.linspace(0, 255, 320, dtype=np.uint8)
        gradient = np.tile(row, (240, 1))
        frame = np.stack([gradient, gradient, gradient], axis=-1)
        return frame

    def close(self) -> None:
        """No-op: there is no underlying resource to release."""

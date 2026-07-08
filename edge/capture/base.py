"""Capture-source interface for the edge tier.

Everything downstream — the HTTP server now, and future clip / motion / stream
stages — consumes frames through CaptureSource and knows nothing about the
specific camera backend. See docs/ARCHITECTURE.md (Camera source) and
docs/specs/2026-07-07-edge-stills-mvp.md.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


class CaptureError(Exception):
    """Raised when a capture source cannot produce a frame."""


class CaptureSource(ABC):
    """A pluggable source of decoded camera frames."""

    @abstractmethod
    def read(self) -> "np.ndarray":
        """Return one decoded BGR frame as a numpy ndarray.

        Raises CaptureError if a frame cannot be produced. Once close() has been
        called the source is poisoned: read() must raise CaptureError and must
        not reopen the underlying resource.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any underlying resources. Safe to call more than once.

        After close(), a later read() raises CaptureError rather than lazily
        reopening — a swapped-out source can never resurrect its handle.
        """

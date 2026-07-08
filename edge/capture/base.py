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

    # -- optional focus control -------------------------------------------
    # Not every camera has a controllable lens, so these are concrete no-op
    # defaults rather than abstract methods: a backend without focus (USB, the
    # fake, a Camera Module 1/2) inherits "no focus" and needs no code. Only the
    # CSI Module-3 backend overrides them. The Config UI shows a focus control
    # ONLY when ``focus_capabilities()`` is non-None, exactly as ARCHITECTURE.md
    # ("Camera source") prescribes capability-driven UI.

    def focus_capabilities(self) -> "dict | None":
        """Describe manual-focus support, or None if the source has no focus.

        Returns ``{"min": float, "max": float}`` — the inclusive lens-position
        range in dioptres — when manual focus is controllable, else None. May
        open the camera to interrogate it. Default: None (unsupported).
        """
        return None

    def set_focus(self, focus: "float | None") -> None:
        """Apply a focus setting; a no-op on sources without focus control.

        ``None`` selects continuous autofocus; a number locks manual focus at
        that many dioptres. Focus is best-effort and never on the frame-delivery
        path, so an unsupported or failed apply is silent. Safe to call before
        the camera is open — the value is applied when it next opens.
        """

    def autofocus_once(self) -> "float | None":
        """Run a single autofocus cycle and lock the lens at the result.

        Returns the resulting lens position in dioptres (and switches the source
        to manual focus locked there), or None if the source has no autofocus.
        Raises CaptureError if a supported autofocus cycle fails. Default: None.
        """
        return None

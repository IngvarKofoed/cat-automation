"""Compute-tier web surface: the FastAPI app and its static frontend.

The first compute-side web app — the frame-collection browser that seeds the
later dashboard. It serves the browse UI, JSON endpoints over the ``Store``, and
the collected JPEGs, and (in normal operation) owns the background collector
thread. See ``docs/specs/2026-07-09-frame-collection-browser.md`` and
``compute/api/app.py``.
"""
from __future__ import annotations

"""Cross-tier contracts shared by the edge and compute tiers.

Deliberately tiny: only the definitions both tiers must agree on live here (the
edge↔compute wire format, the data model, constants). Nothing tier-specific — no
``Picamera2``, no ``torch`` — so importing ``shared`` never drags in a heavy
dependency. See ``shared/CLAUDE.md`` and ``docs/ARCHITECTURE.md``.

This file exists to make ``shared`` an importable package so both
``edge.server`` and ``compute.ingest`` can ``import shared.wire``.
"""
from __future__ import annotations

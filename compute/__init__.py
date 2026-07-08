"""The compute tier — the NVIDIA PC "brain" that connects to the Pi edge.

All the intelligence lives here (detection, tracking, identification, the
decision engine, the event store, notifications, the dashboard, and the learning
loop); the Pi holds no models. This package is the client side of the
edge↔compute split described in ``docs/ARCHITECTURE.md``: it dials *out* to the
Pi's HTTP surfaces, which is why every connection is initiated here and never by
the edge.

This file only exists to make ``compute`` an importable package so
``compute.ingest`` (and later sibling packages) resolve. See ``compute/CLAUDE.md``.
"""
from __future__ import annotations

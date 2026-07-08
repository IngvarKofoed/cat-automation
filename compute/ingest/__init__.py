"""Compute-tier ingest: the client that pulls the Pi edge's data plane.

The first compute-side code. It opens the Pi's continuous ``GET /stream``
(``multipart/x-mixed-replace`` MJPEG), parses each part into a decoded-on-demand
frame plus its motion / frame-id / timestamp metadata, and reads ``GET /status``
as the authoritative camera-health and liveness signal. It stops at "frames +
metadata + health, out" — detection, tracking, and identification are the
*consumers* of these frames in later specs, not part of ingest.

The public surface is deliberately tiny: ``EdgeClient`` (the connection),
``StreamFrame`` (one yielded frame + its metadata), and ``EdgeUnavailable`` (the
liveness signal — a dropped stream or a failed poll means the Pi/network is
down). See ``docs/specs/2026-07-08-compute-ingest-stream-client.md``.
"""
from __future__ import annotations

from compute.ingest.client import EdgeClient, EdgeUnavailable, StreamFrame

__all__ = ["EdgeClient", "EdgeUnavailable", "StreamFrame"]

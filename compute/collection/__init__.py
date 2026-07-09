"""Compute-tier frame collection: the bounded local store + the collector loop.

The first compute-side *persistence*. It saves EVERY frame off the edge stream
— motion and non-motion alike — into a size-bounded local store tagged with its
motion flag and area, so the edge motion gate can be tuned by looking at what it
missed (non-motion, high area) as well as what it wrongly flagged (motion, low
area). Non-motion frames are kept on purpose: they are exactly where missed cats
hide. See ``docs/specs/2026-07-09-frame-collection-browser.md``.

The public surface is two pieces: ``Store`` (the SQLite index + media dir +
retention + clear) and ``run_collector`` (the background loop that pulls the
existing ``EdgeClient.iter_stream_reconnecting()`` feed into the store). The web
app that browses the result lives in ``compute/api``.
"""
from __future__ import annotations

from compute.collection.collector import run_collector
from compute.collection.store import Store

__all__ = ["Store", "run_collector"]

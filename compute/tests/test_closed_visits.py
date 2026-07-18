"""Tests for ``Store.closed_visits`` (compute/collection/store.py).

``closed_visits`` is the live-identify worker's read: the ``(start_id, end_id)``
spans of *settled* motion clusters after a watermark. It reuses the same
``_gap_split`` / ``_VISIT_GAP_MS`` primitive ``events()``/``visits()`` cluster
with, so a worker's visit boundaries can never drift from the activity feed's; it
adds a "closed" filter (the trailing, still-open cluster is excluded until no
later frame can extend it) and a strict ``id > since_id`` watermark floor.

These tests build ``StreamFrame`` objects directly (no cv2, no model, no
network), matching the style of ``test_events.py``.
"""
from __future__ import annotations

from compute.collection.store import Store, _VISIT_GAP_MS
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal valid JPEG — the store writes it verbatim and never decodes it.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(
    frame_id: int = 1,
    ts: int = 1_000,
    motion: bool = False,
    bbox=None,
    area: float = 0.0,
    body: bytes = _JPEG_BODY,
) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, body)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def test_closed_visits_clusters_returned_oldest_first(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Cluster A: two motion frames within the gap -> one cluster.
    a1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    a2 = store.add(
        _frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=base + (_VISIT_GAP_MS - 100)
    )
    # Cluster B: a later motion frame, separated by more than the gap.
    b_ts = base + (_VISIT_GAP_MS - 100) + _VISIT_GAP_MS + 100
    b1 = store.add(_frame(frame_id=3, motion=True, area=0.1), recv_ts_ms=b_ts)

    # "now" well past both clusters -> both are settled/closed.
    now_ms = b_ts + 10 * _VISIT_GAP_MS
    spans = store.closed_visits(None, now_ms)

    # Oldest-first (start_id ASC): cluster A before cluster B.
    assert spans == [(a1, a2), (b1, b1)]


def test_closed_visits_excludes_open_trailing_cluster(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # A settled cluster in the past.
    a1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    a2 = store.add(_frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=base + 500)
    # A trailing cluster whose newest frame is still inside the trailing gap.
    recent_ts = base + 100 * _VISIT_GAP_MS
    store.add(_frame(frame_id=3, motion=True, area=0.1), recv_ts_ms=recent_ts)

    # "now" is only 100 ms after the trailing frame -> within gap_ms -> still open,
    # a later frame could still merge into it, so it must be excluded.
    now_ms = recent_ts + 100
    spans = store.closed_visits(None, now_ms)
    assert spans == [(a1, a2)]


def test_closed_visits_closed_at_gap_boundary_is_strict(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    last_ts = base + 500
    a1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    a2 = store.add(_frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=last_ts)

    # Exactly at the boundary: last recv_ts == now_ms - gap_ms. Closed requires
    # strictly older (< cutoff), so at the boundary the cluster is still OPEN.
    at_boundary = last_ts + _VISIT_GAP_MS
    assert store.closed_visits(None, at_boundary) == []
    # One ms later the cluster's last frame is strictly older than the cutoff.
    assert store.closed_visits(None, at_boundary + 1) == [(a1, a2)]


def test_closed_visits_since_id_floors_strictly(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Cluster A (older).
    store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    a2 = store.add(_frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=base + 300)
    # Cluster B (later, separated by more than the gap).
    b_ts = base + 3 * _VISIT_GAP_MS
    b1 = store.add(_frame(frame_id=3, motion=True, area=0.1), recv_ts_ms=b_ts)
    b2 = store.add(_frame(frame_id=4, motion=True, area=0.1), recv_ts_ms=b_ts + 300)

    now_ms = b_ts + 10 * _VISIT_GAP_MS
    # Watermark AT cluster A's last id -> strict ``id > since_id`` excludes A wholly
    # and every id up to and including a2; only cluster B remains.
    spans = store.closed_visits(a2, now_ms)
    assert spans == [(b1, b2)]


def test_closed_visits_span_is_min_max_motion_id_only(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    m1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    # Non-motion frame within the gap: excluded from the motion cluster, though its
    # id falls inside the returned span.
    store.add(_frame(frame_id=2, motion=False), recv_ts_ms=base + 300)
    m3 = store.add(_frame(frame_id=3, motion=True, area=0.2), recv_ts_ms=base + 600)

    now_ms = base + 10 * _VISIT_GAP_MS
    spans = store.closed_visits(None, now_ms)
    # start_id/end_id are the min/max of the MOTION frames only.
    assert spans == [(m1, m3)]


def test_closed_visits_ignores_non_motion_only_store(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    store.add(_frame(frame_id=1, motion=False), recv_ts_ms=base)
    store.add(_frame(frame_id=2, motion=False), recv_ts_ms=base + 300)
    now_ms = base + 10 * _VISIT_GAP_MS
    assert store.closed_visits(None, now_ms) == []


def test_closed_visits_empty_store(tmp_path):
    store = _store(tmp_path)
    assert store.closed_visits(None, 1_700_000_000_000) == []

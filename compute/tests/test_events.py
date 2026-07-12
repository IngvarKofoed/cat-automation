"""Tests for the activity-page feed: ``Store.events`` and ``GET /api/events``
(compute/collection/store.py, compute/api/app.py).

``events`` is the oracle-free, user-facing cousin of ``visits``: it clusters
``frames.motion = 1`` rows with the same ``_gap_split``/``_VISIT_GAP_MS``
primitive ``visits`` uses, and needs no oracle sweep at all. These tests build
``StreamFrame`` objects directly (no cv2, no model, no network), matching the
style of ``test_motion_workflow.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


# --- Store.events ---------------------------------------------------------


def test_events_clusters_within_gap_and_splits_across_gap(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Two motion frames close together (within _VISIT_GAP_MS) -> one event.
    a1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    a2 = store.add(_frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=base + (_VISIT_GAP_MS - 100))
    # A third motion frame far past the gap -> a separate event.
    b1 = store.add(
        _frame(frame_id=3, motion=True, area=0.1), recv_ts_ms=base + (_VISIT_GAP_MS - 100) + _VISIT_GAP_MS + 100
    )

    result = store.events(None, None)
    events = result["events"]
    assert result["truncated"] is False
    assert len(events) == 2
    # Newest-first: the later event (b1) comes first.
    assert events[0]["start_id"] == b1 and events[0]["end_id"] == b1
    assert events[0]["n_frames"] == 1
    assert events[1]["start_id"] == a1 and events[1]["end_id"] == a2
    assert events[1]["n_frames"] == 2


def test_events_only_motion_frames_define_cluster_but_span_includes_non_motion(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    m1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    # Non-motion frame in between: excluded from clustering, but its id falls
    # inside the eventual [start_id, end_id] span.
    store.add(_frame(frame_id=2, motion=False), recv_ts_ms=base + 500)
    m2 = store.add(_frame(frame_id=3, motion=True, area=0.2), recv_ts_ms=base + 1000)

    result = store.events(None, None)
    events = result["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["n_frames"] == 2  # only the two motion frames counted
    assert ev["start_id"] == m1 and ev["end_id"] == m2  # span covers the gap frame too


def test_events_min_frames_drops_small_clusters(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Small cluster: a single motion frame.
    store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    # Big cluster: three motion frames close together, well after the first.
    big_base = base + 10 * _VISIT_GAP_MS
    b1 = store.add(_frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=big_base)
    store.add(_frame(frame_id=3, motion=True, area=0.1), recv_ts_ms=big_base + 100)
    b3 = store.add(_frame(frame_id=4, motion=True, area=0.1), recv_ts_ms=big_base + 200)

    result = store.events(None, None, min_frames=2)
    events = result["events"]
    assert len(events) == 1
    assert events[0]["start_id"] == b1 and events[0]["end_id"] == b3
    assert events[0]["n_frames"] == 3


def test_events_rep_frame_id_is_max_area_frame(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    f2 = store.add(_frame(frame_id=2, motion=True, area=0.9), recv_ts_ms=base + 100)
    store.add(_frame(frame_id=3, motion=True, area=0.3), recv_ts_ms=base + 200)

    result = store.events(None, None)
    events = result["events"]
    assert len(events) == 1
    assert events[0]["rep_frame_id"] == f2


def test_events_newest_first_ordering(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Three well-separated single-frame events, added in chronological order.
    e1 = store.add(_frame(frame_id=1, motion=True, area=0.1), recv_ts_ms=base)
    e2 = store.add(_frame(frame_id=2, motion=True, area=0.1), recv_ts_ms=base + 5 * _VISIT_GAP_MS)
    e3 = store.add(_frame(frame_id=3, motion=True, area=0.1), recv_ts_ms=base + 10 * _VISIT_GAP_MS)

    result = store.events(None, None)
    events = result["events"]
    assert [ev["start_id"] for ev in events] == [e3, e2, e1]
    # start_ts is strictly decreasing across the list.
    assert events[0]["start_ts"] > events[1]["start_ts"] > events[2]["start_ts"]


def test_events_scoped_by_since_and_until_id(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = []
    for i in range(5):
        fid = store.add(
            _frame(frame_id=i, motion=True, area=0.1), recv_ts_ms=base + i * 10 * _VISIT_GAP_MS
        )
        ids.append(fid)

    scoped = store.events(ids[1], ids[3])["events"]
    assert {ev["start_id"] for ev in scoped} == set(ids[1:4])
    assert len(scoped) == 3


def test_events_truncated_when_over_limit(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Three well-separated single-frame clusters; cap at 2 to force truncation.
    for i in range(3):
        store.add(
            _frame(frame_id=i, motion=True, area=0.1), recv_ts_ms=base + i * 10 * _VISIT_GAP_MS
        )

    result = store.events(None, None, limit=2)
    assert result["truncated"] is True
    assert len(result["events"]) == 2

    result_full = store.events(None, None, limit=10)
    assert result_full["truncated"] is False
    assert len(result_full["events"]) == 3


# --- GET /api/events --------------------------------------------------------


@pytest.fixture
def api_client(tmp_path):
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


def test_api_events_returns_events_and_truncated(api_client):
    client, store = api_client
    base = 1_700_000_000_000
    store.add(_frame(frame_id=1, motion=True, area=0.2), recv_ts_ms=base)
    store.add(_frame(frame_id=2, motion=True, area=0.4), recv_ts_ms=base + 100)

    resp = client.get("/api/events")
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body and "truncated" in body
    assert body["truncated"] is False
    assert len(body["events"]) == 1
    assert body["events"][0]["n_frames"] == 2


def test_api_events_inverted_range_is_400(api_client):
    client, _store = api_client
    resp = client.get("/api/events", params={"since_id": 90, "until_id": 10})
    assert resp.status_code == 400


def test_api_events_min_frames_clamps_to_at_least_one(api_client):
    client, store = api_client
    base = 1_700_000_000_000
    store.add(_frame(frame_id=1, motion=True, area=0.2), recv_ts_ms=base)

    # A non-positive min_frames must still yield the single-frame event (clamped up to 1),
    # not an empty result.
    resp = client.get("/api/events", params={"min_frames": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["n_frames"] == 1

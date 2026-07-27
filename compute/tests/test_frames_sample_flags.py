"""Tests for the ``flags`` review-marker overlay on ``GET /api/frames/sample`` and
``Store.sample_frames`` (compute/api/app.py, compute/collection/store.py).

The admin-next Frame-review grid outlines each tile by two per-frame markers: whether
the edge motion gate fired (``motion``) and whether the frame is corrupt (``corrupt``).
``corrupt`` is deliberately TRI-STATE — ``None`` means no corruption sweep has reached
the frame, which must never be shown as "clean" (the corruption page's
empty-danger-set-reads-as-safe trap, changelog 97).

Pure-sqlite: no torch/ultralytics/GPU. Conventions follow
test_frames_sample_identity.py.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int, motion: bool) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=frame_id * 1_000, motion=motion, bbox=None, area=0.0)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


class _FakeClient:
    def close(self):
        pass


def _client(store: Store) -> TestClient:
    from compute.api.app import create_app

    app = create_app(store=store, client=_FakeClient(), start_collector=False)
    return TestClient(app)


# --- Store.sample_frames(flags=...) ----------------------------------------


def test_no_flags_shape_is_unchanged(tmp_path):
    """Without the flag the row is EXACTLY {id, recv_ts, url} — the density viewers."""
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    (row,) = store.sample_frames(None, None, 100)
    assert set(row) == {"id", "recv_ts", "url"}


def test_flags_reports_motion_per_frame(tmp_path):
    """`motion` comes straight off frames.motion, per sampled frame."""
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    store.add(_frame(2, False), recv_ts_ms=2_000)
    rows = store.sample_frames(None, None, 100, flags=True)
    assert [(r["id"], r["motion"]) for r in rows] == [(1, True), (2, False)]
    # Every row carries the key, so a caller never has to guess.
    assert all("corrupt" in r for r in rows)


def test_unswept_frame_reports_corrupt_none_not_false(tmp_path):
    """No corruption verdict → None (unmeasured), NEVER False (proven clean).

    This is the load-bearing distinction: rendering an unmeasured frame as clean is
    exactly the "an empty danger set reads as safe" failure the corruption review
    page exists to avoid.
    """
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    (row,) = store.sample_frames(None, None, 100, flags=True)
    assert row["corrupt"] is None


def test_flags_reports_stored_corruption_verdict(tmp_path):
    """A swept frame reports its verdict: True for corrupt, False for clean."""
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    store.add(_frame(2, True), recv_ts_ms=2_000)
    store.write_analysis(1, "corruption", verdict=True, score=0.9, detail=None)
    store.write_analysis(2, "corruption", verdict=False, score=0.0, detail=None)
    rows = {r["id"]: r["corrupt"] for r in store.sample_frames(None, None, 100, flags=True)}
    assert rows == {1: True, 2: False}


def test_flags_composes_with_detections_and_identify(tmp_path):
    """The three overlays are independent — asking for flags keeps the others intact."""
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    store.write_analysis(
        1, "yolo-serial", verdict=True, score=0.8,
        detail={"boxes": [[10, 20, 30, 40, 0.8, 15]]},
    )
    (row,) = store.sample_frames(None, None, 100, detections="yolo-serial", flags=True)
    assert row["motion"] is True and row["corrupt"] is None
    assert row["analyzed"] is True and row["cls"] == 15 and row["box"] == [10.0, 20.0, 30.0, 40.0]


# --- GET /api/frames/sample?flags= -----------------------------------------


def test_api_flags_off_by_default(tmp_path):
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    body = _client(store).get("/api/frames/sample?count=10").json()
    assert set(body["frames"][0]) == {"id", "recv_ts", "url"}


def test_api_flags_attaches_markers(tmp_path):
    store = _store(tmp_path)
    store.add(_frame(1, False), recv_ts_ms=1_000)
    store.write_analysis(1, "corruption", verdict=True, score=0.9, detail=None)
    (row,) = _client(store).get("/api/frames/sample?count=10&flags=true").json()["frames"]
    assert row["motion"] is False
    assert row["corrupt"] is True


def test_api_flags_ignored_on_the_density_path(tmp_path):
    """per_ms takes the interval sampler, which carries no overlays."""
    store = _store(tmp_path)
    store.add(_frame(1, True), recv_ts_ms=1_000)
    (row,) = _client(store).get("/api/frames/sample?per_ms=1000&flags=true").json()["frames"]
    assert "motion" not in row

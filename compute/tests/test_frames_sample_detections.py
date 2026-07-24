"""Tests for the playback filmstrip/box addition to ``GET /api/frames/sample``
and ``Store.sample_frames`` (compute/api/app.py, compute/collection/store.py).

See docs/specs/2026-07-24-playback-yolo-boxes.md. The players fetch the exact
frames they show via ``/api/frames/sample``; when they pass
``detections=yolo-serial`` the store attaches each sampled frame's stored
highest-confidence {cat, person, bird} box + score + class so the tile can be
colored and the box drawn — no re-detection.

Pure-sqlite: NO torch/ultralytics/GPU. ``yolo-serial`` verdicts are inserted
directly via ``Store.write_analysis`` with a crafted ``detail['boxes']`` (the
``_boxes_detail`` pattern from test_event_subject_classification.py), and the app
is built through ``create_app`` + ``TestClient`` with a ``FakeClient`` edge.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

_CAT, _PERSON, _BIRD = 15, 0, 14


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = False) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=0.0)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _boxes_detail(boxes: "list[list[float]]") -> dict:
    return {"boxes": boxes}


class _FakeClient:
    """A no-op edge stand-in so create_app's collector wiring has a client."""

    def close(self):
        pass


def _client(store: Store) -> TestClient:
    from compute.api.app import create_app

    app = create_app(store=store, client=_FakeClient(), start_collector=False)
    return TestClient(app)


# --- Store.sample_frames ---------------------------------------------------


def test_sample_frames_no_detections_is_unchanged_shape(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[1, 2, 3, 4, 0.9, _CAT]]))

    rows = store.sample_frames(None, None, 100)
    assert len(rows) == 1
    # EXACTLY {id, recv_ts, url} — no detection keys leak when detections is None.
    assert set(rows[0].keys()) == {"id", "recv_ts", "url"}
    assert rows[0]["id"] == f1
    assert rows[0]["url"] == f"/media/{f1}"


def test_sample_frames_detections_attaches_best_box(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Swept, cat box present (plus a lower-conf person box -> cat wins on conf).
    f_cat = store.add(_frame(frame_id=1), recv_ts_ms=base)
    store.write_analysis(
        f_cat, "yolo-serial", True, 0.9,
        _boxes_detail([[10, 20, 30, 40, 0.9, _CAT], [0, 0, 5, 5, 0.5, _PERSON]]),
    )
    # Swept, NO detection box (empty boxes list) -> analyzed True, everything None.
    f_empty = store.add(_frame(frame_id=2), recv_ts_ms=base + 1)
    store.write_analysis(f_empty, "yolo-serial", False, 0.0, _boxes_detail([]))
    # UN-swept: no analysis row at all -> analyzed False.
    f_unswept = store.add(_frame(frame_id=3), recv_ts_ms=base + 2)

    rows = {r["id"]: r for r in store.sample_frames(None, None, 100, detections="yolo-serial")}

    r = rows[f_cat]
    assert set(r.keys()) == {"id", "recv_ts", "url", "analyzed", "score", "box", "cls"}
    assert r["analyzed"] is True
    assert r["box"] == [10.0, 20.0, 30.0, 40.0]
    assert r["score"] == 0.9
    assert r["cls"] == _CAT

    r = rows[f_empty]
    assert r["analyzed"] is True
    assert r["box"] is None
    assert r["score"] is None
    assert r["cls"] is None

    r = rows[f_unswept]
    assert r["analyzed"] is False
    assert r["box"] is None
    assert r["score"] is None
    assert r["cls"] is None


def test_sample_frames_detections_person_box_wins_when_highest_conf(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f = store.add(_frame(frame_id=1), recv_ts_ms=base)
    # Person is the highest-confidence {cat, person, bird} box -> it is returned,
    # honest about the frame regardless of any visit-level cat chip.
    store.write_analysis(
        f, "yolo-serial", False, 0.0,
        _boxes_detail([[0, 0, 4, 4, 0.4, _CAT], [1, 1, 9, 9, 0.95, _PERSON]]),
    )
    (r,) = store.sample_frames(None, None, 100, detections="yolo-serial")
    assert r["cls"] == _PERSON
    assert r["score"] == 0.95
    assert r["box"] == [1.0, 1.0, 9.0, 9.0]


# --- GET /api/frames/sample -----------------------------------------------


def test_api_frames_sample_without_detections_unchanged(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[1, 2, 3, 4, 0.9, _CAT]]))

    resp = _client(store).get("/api/frames/sample")
    assert resp.status_code == 200
    frames = resp.json()["frames"]
    assert len(frames) == 1
    assert set(frames[0].keys()) == {"id", "recv_ts", "url"}


def test_api_frames_sample_with_detections_attaches(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f_cat = store.add(_frame(frame_id=1), recv_ts_ms=base)
    store.write_analysis(f_cat, "yolo-serial", True, 0.8, _boxes_detail([[5, 6, 7, 8, 0.8, _CAT]]))
    f_empty = store.add(_frame(frame_id=2), recv_ts_ms=base + 1)
    store.write_analysis(f_empty, "yolo-serial", False, 0.0, _boxes_detail([]))
    f_unswept = store.add(_frame(frame_id=3), recv_ts_ms=base + 2)

    resp = _client(store).get("/api/frames/sample", params={"detections": "yolo-serial"})
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()["frames"]}

    assert rows[f_cat]["analyzed"] is True
    assert rows[f_cat]["box"] == [5.0, 6.0, 7.0, 8.0]
    assert rows[f_cat]["score"] == 0.8
    assert rows[f_cat]["cls"] == _CAT

    assert rows[f_empty]["analyzed"] is True
    assert rows[f_empty]["box"] is None
    assert rows[f_empty]["score"] is None

    assert rows[f_unswept]["analyzed"] is False
    assert rows[f_unswept]["box"] is None


def test_api_frames_sample_invalid_analyzer_is_400(tmp_path):
    store = _store(tmp_path)
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    resp = _client(store).get("/api/frames/sample", params={"detections": "not-an-oracle"})
    assert resp.status_code == 400

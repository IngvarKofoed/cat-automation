"""Tests for the cat-identity annotation tool (compute/collection/store.py's
``cats``/``dataset_items`` layer, compute/dataset/crops.py, and the
``/api/cats``, ``/api/label*`` routes on compute/api/app.py).

See docs/specs/2026-07-15-annotation-tool.md. Mirrors the existing suite's
conventions (test_collection.py for plain Store tests, test_api_analysis.py for
the ``create_app(store=..., start_collector=False)`` TestClient pattern): a
``_frame()`` helper builds ``StreamFrame``s directly for Store-level tests that
never decode; a ``_jpeg_gray`` helper builds a REAL, cv2-decodable JPEG for the
crop/label routes, which genuinely ``cv2.imdecode`` stored bytes.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for crop/label tests")

# A minimal but genuinely valid JPEG (SOI ... EOI) for tests that never crop/decode.
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


def _jpeg_gray(level: int = 128, size: int = 64) -> bytes:
    """A real solid-gray JPEG, decodable by cv2 — the crop endpoints need one."""
    img = np.full((size, size, 3), level, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _boxes_detail(boxes: "list[list[float]]") -> dict:
    """An ``analysis.detail`` payload shaped like the yolo-serial oracle's:
    ``{"boxes": [[x1,y1,x2,y2,conf], ...]}``."""
    return {"boxes": boxes}


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


# --- Schema --------------------------------------------------------------------


def test_schema_creates_cats_and_dataset_items_tables(tmp_path):
    store = _store(tmp_path)
    tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "cats" in tables
    assert "dataset_items" in tables
    indexes = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_dataset_src" in indexes
    assert "idx_dataset_cat" in indexes


def test_dataset_root_defaults_beside_media_and_is_created(tmp_path):
    store = _store(tmp_path)
    assert store.dataset_root == os.path.join(str(tmp_path), "dataset")
    assert os.path.isdir(store.dataset_root)


def test_dataset_root_explicit_override(tmp_path):
    custom = str(tmp_path / "elsewhere")
    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
        dataset_root=custom,
    )
    assert store.dataset_root == custom
    assert os.path.isdir(custom)


# --- Cats CRUD -------------------------------------------------------------


def test_create_and_list_cats_ordered_by_id(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("Mittens", is_resident=True)
    b = store.create_cat("Whiskers")

    assert a["is_resident"] is True
    assert a["active"] is True
    assert b["is_resident"] is False

    cats = store.list_cats()
    assert [c["id"] for c in cats] == [a["id"], b["id"]]
    assert [c["name"] for c in cats] == ["Mittens", "Whiskers"]


def test_create_cat_duplicate_name_raises(tmp_path):
    store = _store(tmp_path)
    store.create_cat("Mittens")
    with pytest.raises(ValueError):
        store.create_cat("Mittens")


def test_create_cat_empty_name_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_cat("   ")


def test_update_cat_rename_and_toggle_fields(tmp_path):
    store = _store(tmp_path)
    cat = store.create_cat("Mittens")

    updated = store.update_cat(cat["id"], {"name": "Mittens II", "is_resident": True})
    assert updated["name"] == "Mittens II"
    assert updated["is_resident"] is True
    assert updated["active"] is True  # untouched field unchanged

    retired = store.update_cat(cat["id"], {"active": False})
    assert retired["active"] is False
    assert retired["name"] == "Mittens II"  # PATCH is partial, name unaffected


def test_update_cat_duplicate_name_raises(tmp_path):
    store = _store(tmp_path)
    store.create_cat("Mittens")
    b = store.create_cat("Whiskers")
    with pytest.raises(ValueError):
        store.update_cat(b["id"], {"name": "Mittens"})


def test_update_cat_unknown_id_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.update_cat(999, {"name": "Nope"})


def test_update_cat_empty_fields_raises(tmp_path):
    store = _store(tmp_path)
    cat = store.create_cat("Mittens")
    with pytest.raises(ValueError):
        store.update_cat(cat["id"], {})


def test_list_cats_order_stable_across_add_and_rename(tmp_path):
    # The digit-key binding a caller derives from list_cats() must never shift
    # under a mid-session add/rename — order is creation (id ASC), not name.
    store = _store(tmp_path)
    a = store.create_cat("Alpha")
    b = store.create_cat("Beta")
    store.update_cat(a["id"], {"name": "Zzz-renamed"})
    c = store.create_cat("Gamma")

    cats = store.list_cats()
    assert [c_["id"] for c_ in cats] == [a["id"], b["id"], c["id"]]


# --- add_dataset_items: validation + dedup on (src_frame_id, src_recv_ts) ------


def test_add_dataset_items_inserts_rows_and_returns_count(tmp_path):
    store = _store(tmp_path)
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)

    inserted = store.add_dataset_items(
        [
            {
                "frame_id": fid,
                "label_kind": "identified",
                "cat_id": 1,
                "quality": "gallery",
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "crop_path": "cat_1/x.jpg",
            }
        ]
    )
    assert inserted == 1
    row = store._conn.execute(
        "SELECT cat_id, label_kind, quality, bbox, crop_path, src_frame_id, src_recv_ts, source"
        " FROM dataset_items"
    ).fetchone()
    assert row == (1, "identified", "gallery", "1.0,2.0,3.0,4.0", "cat_1/x.jpg", fid, 1_700_000_000_000, "detector")


def test_add_dataset_items_empty_list_is_noop(tmp_path):
    store = _store(tmp_path)
    assert store.add_dataset_items([]) == 0


def test_add_dataset_items_bad_label_kind_raises_before_any_write(tmp_path):
    store = _store(tmp_path)
    fid1 = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    fid2 = store.add(_frame(frame_id=2), recv_ts_ms=1_700_000_000_001)

    with pytest.raises(ValueError):
        store.add_dataset_items(
            [
                {"frame_id": fid1, "label_kind": "not_cat"},
                {"frame_id": fid2, "label_kind": "bogus_kind"},
            ]
        )
    # Whole batch validated up front — the first (valid) row must NOT have landed.
    (count,) = store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()
    assert count == 0


def test_add_dataset_items_bad_quality_raises(tmp_path):
    store = _store(tmp_path)
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    with pytest.raises(ValueError):
        store.add_dataset_items(
            [{"frame_id": fid, "label_kind": "identified", "cat_id": 1, "quality": "blurry"}]
        )


def test_add_dataset_items_skips_evicted_frame(tmp_path):
    # frame_id no longer live (never existed / already evicted) is SKIPPED, not
    # inserted — its src_recv_ts can't be resolved.
    store = _store(tmp_path)
    inserted = store.add_dataset_items(
        [{"frame_id": 999999, "label_kind": "not_cat"}]
    )
    assert inserted == 0
    (count,) = store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()
    assert count == 0


def test_dedup_survives_clear_and_rowid_reuse(tmp_path):
    # The load-bearing durability guarantee: after clear() + rowid reuse, an old
    # label for the previous occupant of an id must NOT mask a brand-new frame
    # that happens to reuse that id, because src_recv_ts won't match.
    store = _store(tmp_path)
    old_id = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(old_id, "yolo-serial", True, 0.9, _boxes_detail([[1, 1, 10, 10, 0.9]]))
    store.add_dataset_items([{"frame_id": old_id, "label_kind": "not_cat"}])

    # Confirm it is excluded from the queue before the clear.
    visits = store.annotation_visits("yolo-serial")
    assert visits == []

    store.clear()  # wipes frames + analysis; dataset_items/cats survive; rowids reset

    # Re-add a frame — SQLite reuses rowid 1 (AUTOINCREMENT not used), but at a
    # DIFFERENT recv_ts than the old label's src_recv_ts.
    new_id = store.add(_frame(frame_id=1), recv_ts_ms=1_800_000_000_000)
    assert new_id == old_id  # rowid reuse actually happened, or this test proves nothing
    store.write_analysis(new_id, "yolo-serial", True, 0.9, _boxes_detail([[1, 1, 10, 10, 0.9]]))

    # The new frame at the reused id must be treated as UNDECIDED.
    visits = store.annotation_visits("yolo-serial")
    assert len(visits) == 1
    assert visits[0]["frames"][0]["id"] == new_id
    assert visits[0]["frames"][0]["recv_ts"] == 1_800_000_000_000


# --- Durability: cats + dataset_items survive clear() and eviction ------------


def test_cats_and_dataset_items_survive_clear(tmp_path):
    store = _store(tmp_path)
    cat = store.create_cat("Mittens")
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.add_dataset_items(
        [{"frame_id": fid, "label_kind": "identified", "cat_id": cat["id"], "quality": "ok"}]
    )

    deleted = store.clear()
    assert deleted == 1  # the one frame row

    assert store.list_cats() == [cat]
    (count,) = store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()
    assert count == 1


def test_cats_and_dataset_items_survive_eviction(tmp_path):
    # A cap sized for exactly one frame: the first add() survives (so the label
    # can be written against a still-live frame), and later adds evict it.
    # cats/dataset_items carry no FK to frames and must be untouched by eviction.
    store = _store(tmp_path, max_bytes=len(_JPEG_BODY))
    cat = store.create_cat("Mittens")
    fid = store.add(_frame(frame_id=1, body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000)
    store.add_dataset_items([{"frame_id": fid, "label_kind": "not_cat"}])

    # Force eviction of the frame we just labelled by adding more frames under
    # the tiny cap.
    for i in range(2, 6):
        store.add(_frame(frame_id=i, body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000 + i)

    stats = store.stats()
    assert stats["count"] < 5  # eviction genuinely ran

    assert store.list_cats() == [cat]
    (count,) = store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()
    assert count == 1  # the label row for the now-evicted frame remains


# --- annotation_visits: clustering + excluding already-decided ------------------


def test_annotation_visits_clusters_present_frames_chronologically(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Visit 1: two frames 500ms apart (well within _VISIT_GAP_MS=2000).
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    f2 = store.add(_frame(frame_id=2), recv_ts_ms=base + 500)
    # Visit 2: a frame 10s later (beyond the gap) -> a new visit.
    f3 = store.add(_frame(frame_id=3), recv_ts_ms=base + 10_000)

    store.write_analysis(f1, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    store.write_analysis(f2, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 20, 20, 0.9]]))  # bigger box
    store.write_analysis(f3, "yolo-serial", True, 0.7, _boxes_detail([[0, 0, 5, 5, 0.7]]))

    visits = store.annotation_visits("yolo-serial")
    assert len(visits) == 2

    v1 = visits[0]
    assert [fr["id"] for fr in v1["frames"]] == [f1, f2]
    assert v1["rep_frame_id"] == f2  # peak box-area frame (20x20 > 10x10)
    assert v1["peak_area"] == pytest.approx(400.0)
    assert v1["peak_score"] == pytest.approx(0.9)
    assert v1["span"] == [base, base + 500]

    v2 = visits[1]
    assert [fr["id"] for fr in v2["frames"]] == [f3]
    assert v2["rep_frame_id"] == f3

    # Chronological ordering: visit 1 (earlier span) precedes visit 2.
    assert visits[0]["span"][0] < visits[1]["span"][0]


def test_annotation_visits_excludes_labelled_frames(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    f2 = store.add(_frame(frame_id=2), recv_ts_ms=base + 500)
    store.write_analysis(f1, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    store.write_analysis(f2, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))

    # Label f1 only -> it should drop out of the visit, leaving f2 alone.
    store.add_dataset_items([{"frame_id": f1, "label_kind": "not_cat"}])

    visits = store.annotation_visits("yolo-serial")
    assert len(visits) == 1
    assert [fr["id"] for fr in visits[0]["frames"]] == [f2]


def test_annotation_visits_empty_when_no_boxes(tmp_path):
    store = _store(tmp_path)
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    # Present verdict but malformed/empty detail -> no usable box -> dropped.
    store.write_analysis(fid, "yolo-serial", True, 0.5, {"boxes": []})
    assert store.annotation_visits("yolo-serial") == []


def test_annotation_visits_scoped_by_since_until_id(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = []
    for i in range(3):
        fid = store.add(_frame(frame_id=i), recv_ts_ms=base + i * 10_000)
        store.write_analysis(fid, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
        ids.append(fid)

    visits = store.annotation_visits("yolo-serial", since_id=ids[1], until_id=ids[1])
    assert len(visits) == 1
    assert visits[0]["frames"][0]["id"] == ids[1]


def test_annotation_visits_unknown_oracle_not_pre_validated_by_store(tmp_path):
    # The store itself does not gate the oracle name (the route does, per the
    # contract); an unregistered oracle simply yields no rows/visits.
    store = _store(tmp_path)
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    assert store.annotation_visits("nonexistent-oracle") == []


def test_label_progress_counts_visits_and_crops(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    f2 = store.add(_frame(frame_id=2), recv_ts_ms=base + 500)
    f3 = store.add(_frame(frame_id=3), recv_ts_ms=base + 20_000)
    for fid in (f1, f2, f3):
        store.write_analysis(fid, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))

    progress = store.label_progress("yolo-serial")
    assert progress == {"total_visits": 2, "decided_visits": 0, "crops_labeled": 0}

    # Decide the whole first visit (f1, f2).
    store.add_dataset_items(
        [
            {"frame_id": f1, "label_kind": "not_cat"},
            {"frame_id": f2, "label_kind": "not_cat"},
        ]
    )
    progress = store.label_progress("yolo-serial")
    assert progress == {"total_visits": 2, "decided_visits": 1, "crops_labeled": 2}


# --- dataset/crops.py: crop_bytes + materialize --------------------------------


@_requires_cv
def test_crop_bytes_returns_decodable_jpeg(tmp_path):
    from compute.dataset import crops

    jpeg_path = tmp_path / "frame.jpg"
    jpeg_path.write_bytes(_jpeg_gray(200, size=64))

    data = crops.crop_bytes(str(jpeg_path), [10, 10, 40, 40])
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    assert img.shape[:2] == (30, 30)


@_requires_cv
def test_crop_bytes_clamps_box_to_image_bounds(tmp_path):
    from compute.dataset import crops

    jpeg_path = tmp_path / "frame.jpg"
    jpeg_path.write_bytes(_jpeg_gray(200, size=64))

    data = crops.crop_bytes(str(jpeg_path), [-10, -10, 1000, 1000])
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (64, 64)


@_requires_cv
def test_crop_bytes_degenerate_box_raises(tmp_path):
    from compute.dataset import crops

    jpeg_path = tmp_path / "frame.jpg"
    jpeg_path.write_bytes(_jpeg_gray(200, size=64))

    with pytest.raises(ValueError):
        crops.crop_bytes(str(jpeg_path), [10, 10, 10, 50])  # zero width


@_requires_cv
def test_materialize_writes_file_and_returns_true(tmp_path):
    from compute.dataset import crops

    root = tmp_path / "dataset"
    root.mkdir()
    jpeg_path = tmp_path / "frame.jpg"
    jpeg_path.write_bytes(_jpeg_gray(200, size=64))
    dest = root / "cat_1" / "1_100.jpg"

    ok = crops.materialize(str(jpeg_path), [0, 0, 30, 30], str(dest), root=str(root))
    assert ok is True
    assert dest.is_file()


@_requires_cv
def test_materialize_rejects_path_traversal(tmp_path):
    from compute.dataset import crops

    root = tmp_path / "dataset"
    root.mkdir()
    jpeg_path = tmp_path / "frame.jpg"
    jpeg_path.write_bytes(_jpeg_gray(200, size=64))
    escaping_dest = tmp_path / "outside.jpg"  # NOT under root

    ok = crops.materialize(str(jpeg_path), [0, 0, 30, 30], str(escaping_dest), root=str(root))
    assert ok is False
    assert not escaping_dest.is_file()


@_requires_cv
def test_materialize_false_on_degenerate_box(tmp_path):
    from compute.dataset import crops

    root = tmp_path / "dataset"
    root.mkdir()
    jpeg_path = tmp_path / "frame.jpg"
    jpeg_path.write_bytes(_jpeg_gray(200, size=64))
    dest = root / "cat_1" / "1_100.jpg"

    ok = crops.materialize(str(jpeg_path), [10, 10, 10, 10], str(dest), root=str(root))
    assert ok is False
    assert not dest.is_file()


# --- API: create_app wiring ------------------------------------------------------


class _FakeClient:
    """A stand-in edge connection: no network, no real Pi (mirrors test_api_analysis.py)."""

    def iter_stream_reconnecting(self):
        return iter(())


@pytest.fixture
def make_app(tmp_path):
    """Factory for a ``TestClient`` over a fresh ``Store``, mirroring
    test_api_analysis.py's ``make_app`` fixture: no real collector/thread."""

    def _make():
        from compute.api.app import create_app

        store = _store(tmp_path)
        app = create_app(store=store, client=_FakeClient(), start_collector=False)
        return TestClient(app), store

    return _make


def test_boot_smoke_create_app_and_get_cats(tmp_path):
    # Mandated boot smoke: build create_app and hit GET /api/cats via TestClient.
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, client=_FakeClient(), start_collector=False)
    client = TestClient(app)
    resp = client.get("/api/cats")
    assert resp.status_code == 200
    assert resp.json() == {"cats": []}


# --- API: /api/cats CRUD ---------------------------------------------------------


def test_api_cats_create_list_patch(make_app):
    client, _store = make_app()

    resp = client.post("/api/cats", json={"name": "Mittens", "is_resident": True})
    assert resp.status_code == 200
    cat = resp.json()
    assert cat["name"] == "Mittens"
    assert cat["is_resident"] is True

    resp = client.get("/api/cats")
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()["cats"]] == ["Mittens"]

    resp = client.patch(f"/api/cats/{cat['id']}", json={"active": False})
    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert resp.json()["name"] == "Mittens"  # untouched


def test_api_cats_duplicate_name_is_400(make_app):
    client, _store = make_app()
    client.post("/api/cats", json={"name": "Mittens"})
    resp = client.post("/api/cats", json={"name": "Mittens"})
    assert resp.status_code == 400


def test_api_cats_patch_unknown_id_is_400(make_app):
    client, _store = make_app()
    resp = client.patch("/api/cats/999", json={"name": "Nope"})
    assert resp.status_code == 400


def test_api_cats_patch_empty_body_is_400(make_app):
    client, _store = make_app()
    resp = client.post("/api/cats", json={"name": "Mittens"})
    cat_id = resp.json()["id"]
    resp = client.patch(f"/api/cats/{cat_id}", json={})
    assert resp.status_code == 400


# --- API: GET /api/label/visits ---------------------------------------------------


def test_api_label_visits_returns_queue_and_progress(make_app):
    client, store = make_app()
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    store.write_analysis(f1, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))

    resp = client.get("/api/label/visits")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["visits"]) == 1
    assert body["visits"][0]["frames"][0]["id"] == f1
    assert body["total_visits"] == 1
    assert body["decided_visits"] == 0
    assert body["crops_labeled"] == 0


def test_api_label_visits_unknown_oracle_is_400(make_app):
    client, _store = make_app()
    resp = client.get("/api/label/visits", params={"oracle": "bogus"})
    assert resp.status_code == 400


def test_api_label_visits_inverted_bounds_is_400(make_app):
    client, _store = make_app()
    resp = client.get("/api/label/visits", params={"since_id": 90, "until_id": 10})
    assert resp.status_code == 400


# --- API: POST /api/label ---------------------------------------------------------


@_requires_cv
def test_api_label_identified_writes_row_and_crop(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_jpeg_gray(180)), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))

    cat = client.post("/api/cats", json={"name": "Mittens", "is_resident": True}).json()

    resp = client.post(
        "/api/label",
        json={
            "decision": "identified",
            "cat_id": cat["id"],
            "frames": [{"frame_id": fid, "bbox": [0, 0, 40, 40], "quality": "gallery"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 1, "crops": 1}

    row = store._conn.execute(
        "SELECT cat_id, label_kind, quality, crop_path FROM dataset_items"
    ).fetchone()
    assert row[0] == cat["id"]
    assert row[1] == "identified"
    assert row[2] == "gallery"
    crop_path = row[3]
    assert crop_path.startswith(f"cat_{cat['id']}{os.sep}")
    assert os.path.isfile(os.path.join(store.dataset_root, crop_path))

    # The labelled frame must now be gone from the queue.
    visits = client.get("/api/label/visits").json()["visits"]
    assert visits == []


@_requires_cv
def test_api_label_unknown_cat_writes_to_cat_unknown_cat_dir(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_jpeg_gray(180)), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))

    resp = client.post(
        "/api/label",
        json={
            "decision": "unknown_cat",
            "frames": [{"frame_id": fid, "bbox": [0, 0, 40, 40], "quality": "ok"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 1, "crops": 1}

    row = store._conn.execute("SELECT cat_id, label_kind, crop_path FROM dataset_items").fetchone()
    assert row[0] is None
    assert row[1] == "unknown_cat"
    assert row[2].startswith(f"cat_unknown_cat{os.sep}")


def test_api_label_not_cat_writes_row_with_no_crop(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))

    resp = client.post(
        "/api/label",
        json={"decision": "not_cat", "frames": [{"frame_id": fid}]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 1, "crops": 0}

    row = store._conn.execute(
        "SELECT label_kind, quality, bbox, crop_path FROM dataset_items"
    ).fetchone()
    assert row == ("not_cat", None, None, None)


def test_api_label_bad_decision_is_400(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    resp = client.post("/api/label", json={"decision": "bogus", "frames": [{"frame_id": fid}]})
    assert resp.status_code == 400


def test_api_label_identified_requires_cat_id(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    resp = client.post(
        "/api/label", json={"decision": "identified", "frames": [{"frame_id": fid}]}
    )
    assert resp.status_code == 400


def test_api_label_identified_unknown_cat_id_is_400(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    resp = client.post(
        "/api/label",
        json={"decision": "identified", "cat_id": 999, "frames": [{"frame_id": fid}]},
    )
    assert resp.status_code == 400


@_requires_cv
def test_api_label_skips_evicted_frame(make_app):
    client, store = make_app()
    resp = client.post(
        "/api/label",
        json={"decision": "not_cat", "frames": [{"frame_id": 999999}]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 0, "crops": 0}


# --- API: GET /api/label/crop/{frame_id} ------------------------------------------


@_requires_cv
def test_api_label_crop_returns_jpeg_bytes(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_jpeg_gray(180, size=64)), recv_ts_ms=1_700_000_000_000)

    resp = client.get(f"/api/label/crop/{fid}", params={"box": "0,0,30,30"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    img = cv2.imdecode(np.frombuffer(resp.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    assert img.shape[:2] == (30, 30)


def test_api_label_crop_malformed_box_is_400(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000)
    resp = client.get(f"/api/label/crop/{fid}", params={"box": "not,a,box"})
    assert resp.status_code == 400


def test_api_label_crop_unknown_frame_is_404(make_app):
    client, _store = make_app()
    resp = client.get("/api/label/crop/999999", params={"box": "0,0,10,10"})
    assert resp.status_code == 404


@_requires_cv
def test_api_label_crop_degenerate_box_is_400(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_jpeg_gray(180, size=64)), recv_ts_ms=1_700_000_000_000)
    resp = client.get(f"/api/label/crop/{fid}", params={"box": "10,10,10,50"})
    assert resp.status_code == 400


# --- Store: labeled_visits + delete_dataset_items (undo / re-label) ---------------


def test_labeled_visits_returns_decided_visits_with_identity(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    f2 = store.add(_frame(frame_id=2), recv_ts_ms=base + 500)  # within gap → one visit
    store.write_analysis(f1, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    store.write_analysis(f2, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 30, 30, 0.9]]))  # bigger → rep
    cat = store.create_cat("Simba", is_resident=True)
    store.add_dataset_items(
        [
            {"frame_id": f1, "label_kind": "identified", "cat_id": cat["id"],
             "quality": "ok", "bbox": [0, 0, 10, 10], "crop_path": f"cat_{cat['id']}/a.jpg"},
            {"frame_id": f2, "label_kind": "identified", "cat_id": cat["id"],
             "quality": "gallery", "bbox": [0, 0, 30, 30], "crop_path": f"cat_{cat['id']}/b.jpg"},
        ]
    )
    visits = store.labeled_visits("yolo-serial")
    assert len(visits) == 1
    v = visits[0]
    assert v["label_kind"] == "identified"
    assert v["cat_id"] == cat["id"]
    assert v["cat_name"] == "Simba"
    assert v["mixed"] is False
    assert v["rep_frame_id"] == f2  # peak box area
    assert {fr["id"] for fr in v["frames"]} == {f1, f2}


def test_labeled_visits_excludes_undecided_and_queue_excludes_decided(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    f2 = store.add(_frame(frame_id=2), recv_ts_ms=base + 100_000)  # far apart → separate visits
    store.write_analysis(f1, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    store.write_analysis(f2, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    store.add_dataset_items(
        [{"frame_id": f1, "label_kind": "unknown_cat", "cat_id": None,
          "quality": "ok", "bbox": [0, 0, 10, 10], "crop_path": "cat_unknown_cat/a.jpg"}]
    )
    labeled = store.labeled_visits("yolo-serial")
    assert len(labeled) == 1
    assert labeled[0]["frames"][0]["id"] == f1
    assert labeled[0]["label_kind"] == "unknown_cat"
    queue = store.annotation_visits("yolo-serial")
    assert len(queue) == 1
    assert queue[0]["frames"][0]["id"] == f2


def test_delete_dataset_items_requeues_and_returns_crop_paths(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=base)
    store.write_analysis(f1, "yolo-serial", True, 0.5, _boxes_detail([[0, 0, 10, 10, 0.5]]))
    cat = store.create_cat("Simba")
    store.add_dataset_items(
        [{"frame_id": f1, "label_kind": "identified", "cat_id": cat["id"],
          "quality": "ok", "bbox": [0, 0, 10, 10], "crop_path": f"cat_{cat['id']}/a.jpg"}]
    )
    assert store.annotation_visits("yolo-serial") == []  # decided → out of the queue

    removed = store.delete_dataset_items([f1])
    assert removed == [{"frame_id": f1, "crop_path": f"cat_{cat['id']}/a.jpg"}]
    assert store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()[0] == 0
    # frame is back in the queue (undecided again), roster untouched.
    assert len(store.annotation_visits("yolo-serial")) == 1
    assert len(store.list_cats()) == 1


def test_delete_dataset_items_empty_is_noop(tmp_path):
    store = _store(tmp_path)
    assert store.delete_dataset_items([]) == []


# --- API: /api/label/labeled + /api/label/relabel + /api/label/delete -------------


def test_api_label_labeled_lists_labelled_visits(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))
    client.post("/api/label", json={"decision": "not_cat", "frames": [{"frame_id": fid}]})

    resp = client.get("/api/label/labeled")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["visits"][0]["label_kind"] == "not_cat"
    assert body["visits"][0]["frames"][0]["id"] == fid


def test_api_label_labeled_unknown_oracle_is_400(make_app):
    client, _store = make_app()
    assert client.get("/api/label/labeled", params={"oracle": "bogus"}).status_code == 400


@_requires_cv
def test_api_label_relabel_changes_identity_and_moves_crop(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_jpeg_gray(180)), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))
    a = client.post("/api/cats", json={"name": "A"}).json()
    b = client.post("/api/cats", json={"name": "B"}).json()

    client.post(
        "/api/label",
        json={"decision": "identified", "cat_id": a["id"],
              "frames": [{"frame_id": fid, "bbox": [0, 0, 40, 40], "quality": "ok"}]},
    )
    old_path = store._conn.execute("SELECT crop_path FROM dataset_items").fetchone()[0]
    assert os.path.isfile(os.path.join(store.dataset_root, old_path))

    resp = client.post(
        "/api/label/relabel",
        json={"decision": "identified", "cat_id": b["id"],
              "frames": [{"frame_id": fid, "bbox": [0, 0, 40, 40], "quality": "gallery"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1 and resp.json()["inserted"] == 1

    rows = store._conn.execute("SELECT cat_id, quality, crop_path FROM dataset_items").fetchall()
    assert len(rows) == 1  # exactly one row (old deleted, new written), no duplicate
    assert rows[0][0] == b["id"]
    assert rows[0][1] == "gallery"
    new_path = rows[0][2]
    assert new_path.startswith(f"cat_{b['id']}{os.sep}")
    assert os.path.isfile(os.path.join(store.dataset_root, new_path))
    assert not os.path.isfile(os.path.join(store.dataset_root, old_path))  # old crop removed


@_requires_cv
def test_api_label_delete_undoes_and_removes_crop(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1, body=_jpeg_gray(180)), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))
    client.post(
        "/api/label",
        json={"decision": "unknown_cat",
              "frames": [{"frame_id": fid, "bbox": [0, 0, 40, 40], "quality": "ok"}]},
    )
    crop_path = store._conn.execute("SELECT crop_path FROM dataset_items").fetchone()[0]
    assert os.path.isfile(os.path.join(store.dataset_root, crop_path))

    resp = client.post("/api/label/delete", json={"frame_ids": [fid]})
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1, "crops_removed": 1}
    assert store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()[0] == 0
    assert not os.path.isfile(os.path.join(store.dataset_root, crop_path))
    # the frame is back in the labelling queue.
    assert len(client.get("/api/label/visits").json()["visits"]) == 1

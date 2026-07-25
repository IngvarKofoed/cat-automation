"""Tests for the identity READ overlay on ``GET /api/frames/sample`` and
``Store.sample_frames`` (compute/api/app.py, compute/collection/store.py).

The admin-next Frame-review page doubles as a model-evaluation surface: each
sampled frame carries its ACTIVE-gallery nearest match, resolved through the SAME
threshold/fail-safe rule ``events()`` uses (changelog 68/70). ``identify=1``
attaches an ``identity`` key per frame — ``{cat_id, cat_name, is_resident,
distance, resolved}`` (``resolved`` ∈ resident|neighbour|unknown), or ``None`` —
mirroring the ``detections=`` per-frame detection overlay (changelog 111).

Pure-sqlite: NO torch/ultralytics/GPU. Model versions + identifications are
inserted directly (a bare ``gallery.npz`` placeholder makes a version promotable;
the store only checks the file's existence). Conventions follow
test_frames_sample_detections.py + test_identification_store.py.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = True) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=0.0)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _write_gallery_file(store: Store, gallery_dir: str) -> None:
    d = os.path.join(store.models_root, gallery_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gallery.npz"), "wb") as fh:
        fh.write(b"\x00")


def _add_active_model(store: Store, *, threshold: "float | None", gallery_dir: str = "g") -> int:
    """Insert a model version, write its gallery placeholder, and promote it active."""
    _write_gallery_file(store, gallery_dir)
    vid = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=10,
        threshold=threshold,
        quality="gallery",
        metrics=None,
        gallery_dir=gallery_dir,
    )
    store.promote_model(vid)
    return vid


class _FakeClient:
    """A no-op edge stand-in so create_app's collector wiring has a client."""

    def close(self):
        pass


def _client(store: Store) -> TestClient:
    from compute.api.app import create_app

    app = create_app(store=store, client=_FakeClient(), start_collector=False)
    return TestClient(app)


def _boxes_detail(boxes: "list[list[float]]") -> dict:
    return {"boxes": boxes}


# --- Store.sample_frames(identify=...) -------------------------------------


def test_sample_frames_no_identify_is_unchanged_shape(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_identifications_batch([(f1, vid, cat, 0.2, [0, 0, 1, 1])])

    rows = store.sample_frames(None, None, 100)
    assert len(rows) == 1
    # No overlay requested → EXACTLY {id, recv_ts, url}, no identity key leaks.
    assert set(rows[0].keys()) == {"id", "recv_ts", "url"}


def test_sample_frames_identify_no_active_model_yields_none(tmp_path):
    store = _store(tmp_path)
    # A DRAFT (never-promoted) model + identifications: active_model() is None, so
    # every frame's identity is None (the key is present, the value None).
    _write_gallery_file(store, "g")
    vid = store.add_model_version(
        status="draft", kind="gallery", backbone="b", imgsz=224, n_cats=1,
        n_vectors=1, threshold=0.5, quality="gallery", metrics=None, gallery_dir="g",
    )
    cat = store.create_cat("A")["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_identifications_batch([(f1, vid, cat, 0.1, [0, 0, 1, 1])])

    (r,) = store.sample_frames(None, None, 100, identify=True)
    assert set(r.keys()) == {"id", "recv_ts", "url", "identity"}
    assert r["identity"] is None


def test_sample_frames_identify_resident_match(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    mittens = store.create_cat("Mittens", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_identifications_batch([(f1, vid, mittens, 0.2, [0, 0, 1, 1])])

    (r,) = store.sample_frames(None, None, 100, identify=True)
    ident = r["identity"]
    assert set(ident.keys()) == {"cat_id", "cat_name", "is_resident", "distance", "resolved"}
    assert ident["cat_id"] == mittens
    assert ident["cat_name"] == "Mittens"
    assert ident["is_resident"] is True
    assert ident["distance"] == pytest.approx(0.2)
    assert ident["resolved"] == "resident"


def test_sample_frames_identify_neighbour_match(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    tom = store.create_cat("Tom", is_resident=False)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_identifications_batch([(f1, vid, tom, 0.3, [0, 0, 1, 1])])

    (r,) = store.sample_frames(None, None, 100, identify=True)
    ident = r["identity"]
    assert ident["cat_id"] == tom
    assert ident["is_resident"] is False
    assert ident["resolved"] == "neighbour"


def test_sample_frames_identify_unknown_when_beyond_threshold(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.1)  # tight cutoff
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    # Nearest match at 0.5 is beyond 0.1 → an unknown cat, never named.
    store.write_identifications_batch([(f1, vid, cat, 0.5, [0, 0, 1, 1])])

    (r,) = store.sample_frames(None, None, 100, identify=True)
    ident = r["identity"]
    assert ident["resolved"] == "unknown"
    assert ident["cat_id"] is None
    assert ident["cat_name"] is None
    assert ident["is_resident"] is None
    assert ident["distance"] == pytest.approx(0.5)  # nearest distance still reported


def test_sample_frames_identify_uncalibrated_model_fails_safe(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=None)  # uncomputable cutoff
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    # Even a very near match must degrade to unknown — an uncalibrated model must
    # never confidently name a resident (CONCEPT/CLAUDE.md fail-safe).
    store.write_identifications_batch([(f1, vid, cat, 0.01, [0, 0, 1, 1])])

    (r,) = store.sample_frames(None, None, 100, identify=True)
    ident = r["identity"]
    assert ident["resolved"] == "unknown"
    assert ident["cat_id"] is None
    assert ident["distance"] == pytest.approx(0.01)


def test_sample_frames_identify_none_for_unidentified_and_marker_rows(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    cat = store.create_cat("A", is_resident=True)["id"]
    f_named = store.add(_frame(frame_id=1, ts=1_000), recv_ts_ms=1_000)
    f_marker = store.add(_frame(frame_id=2, ts=2_000), recv_ts_ms=2_000)
    f_none = store.add(_frame(frame_id=3, ts=3_000), recv_ts_ms=3_000)
    store.write_identifications_batch(
        [
            (f_named, vid, cat, 0.2, [0, 0, 1, 1]),
            # Marker row: processed but un-embeddable (cat_id NULL, distance NULL).
            (f_marker, vid, None, None, None),
        ]
    )

    rows = {r["id"]: r for r in store.sample_frames(None, None, 100, identify=True)}
    assert rows[f_named]["identity"]["resolved"] == "resident"
    # A marker row carries no identity → identity None, like an un-identified frame.
    assert rows[f_marker]["identity"] is None
    assert rows[f_none]["identity"] is None


def test_sample_frames_identify_composes_with_detections(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[1, 2, 3, 4, 0.9, 15]]))
    store.write_identifications_batch([(f1, vid, cat, 0.2, [0, 0, 1, 1])])

    (r,) = store.sample_frames(None, None, 100, detections="yolo-serial", identify=True)
    # Both overlays attach independently on the same sampled frame.
    assert r["box"] == [1.0, 2.0, 3.0, 4.0]
    assert r["analyzed"] is True
    assert r["identity"]["cat_id"] == cat
    assert r["identity"]["resolved"] == "resident"


def test_sample_frames_identify_only_active_model_idents(tmp_path):
    store = _store(tmp_path)
    # v1 draft with an identification, v2 active with none for the frame → the frame
    # reads unidentified under the active model (idents are per-model).
    _write_gallery_file(store, "g1")
    v1 = store.add_model_version(
        status="draft", kind="gallery", backbone="b", imgsz=224, n_cats=1,
        n_vectors=1, threshold=0.5, quality="gallery", metrics=None, gallery_dir="g1",
    )
    v2 = _add_active_model(store, threshold=0.5, gallery_dir="g2")
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_identifications_batch([(f1, v1, cat, 0.1, [0, 0, 1, 1])])  # only for v1
    assert v2 != v1

    (r,) = store.sample_frames(None, None, 100, identify=True)
    assert r["identity"] is None


# --- GET /api/frames/sample?identify=1 ------------------------------------


def test_api_frames_sample_without_identify_unchanged(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_identifications_batch([(f1, vid, cat, 0.2, [0, 0, 1, 1])])

    resp = _client(store).get("/api/frames/sample")
    assert resp.status_code == 200
    frames = resp.json()["frames"]
    assert len(frames) == 1
    assert set(frames[0].keys()) == {"id", "recv_ts", "url"}


def test_api_frames_sample_with_identify_attaches(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    resident = store.create_cat("Mittens", is_resident=True)["id"]
    neighbour = store.create_cat("Tom", is_resident=False)["id"]
    f_res = store.add(_frame(frame_id=1, ts=1_000), recv_ts_ms=1_000)
    f_nei = store.add(_frame(frame_id=2, ts=2_000), recv_ts_ms=2_000)
    f_unk = store.add(_frame(frame_id=3, ts=3_000), recv_ts_ms=3_000)
    store.write_identifications_batch(
        [
            (f_res, vid, resident, 0.2, [0, 0, 1, 1]),
            (f_nei, vid, neighbour, 0.3, [0, 0, 1, 1]),
            (f_unk, vid, resident, 0.9, [0, 0, 1, 1]),  # beyond 0.5 → unknown
        ]
    )

    resp = _client(store).get("/api/frames/sample", params={"identify": 1})
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()["frames"]}
    assert rows[f_res]["identity"]["resolved"] == "resident"
    assert rows[f_nei]["identity"]["resolved"] == "neighbour"
    assert rows[f_unk]["identity"]["resolved"] == "unknown"
    assert rows[f_unk]["identity"]["cat_id"] is None


def test_api_frames_sample_identify_and_detections_compose(tmp_path):
    store = _store(tmp_path)
    vid = _add_active_model(store, threshold=0.5)
    cat = store.create_cat("A", is_resident=True)["id"]
    f1 = store.add(_frame(frame_id=1), recv_ts_ms=1_000)
    store.write_analysis(f1, "yolo-serial", True, 0.8, _boxes_detail([[5, 6, 7, 8, 0.8, 15]]))
    store.write_identifications_batch([(f1, vid, cat, 0.2, [0, 0, 1, 1])])

    resp = _client(store).get(
        "/api/frames/sample", params={"identify": 1, "detections": "yolo-serial"}
    )
    assert resp.status_code == 200
    (r,) = resp.json()["frames"]
    assert r["box"] == [5.0, 6.0, 7.0, 8.0]
    assert r["identity"]["resolved"] == "resident"

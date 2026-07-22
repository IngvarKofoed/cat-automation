"""Tests for the user dashboard's Cats backend (user-activity-cats spec).

Covers ``Store.cats_overview`` (roster + feed-derived last-seen + has_crop) and
the ``/api/cats/overview`` + avatar routes on ``compute/api/app.py``. Builds a
real temp ``Store`` and a ``TestClient(create_app(...))`` with no edge/thread,
mirroring ``test_events.py`` / ``test_api_identification.py``. Frames the store
only stores verbatim use a fake JPEG; avatar uploads and durable crop files (the
ones actually decoded / re-encoded) are genuine tiny JPEGs made with ``cv2``.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store, _VISIT_GAP_MS
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal valid JPEG the store writes verbatim and never decodes (frame bytes).
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int, ts: int, *, motion: bool = False, area: float = 0.0) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _real_jpeg(color: int = 0) -> bytes:
    """A genuine tiny JPEG (8x8, solid ``color``) — decodable by cv2 for upload/crop tests."""
    img = np.full((8, 8, 3), color, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _write_gallery_npz(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez(
        path,
        vectors=np.zeros((2, 4), dtype=np.float32),
        cat_ids=np.array([1, 2], dtype=np.int64),
        backbone="dinov2_vits14",
        imgsz=224,
    )


def _promote_model(store: Store, threshold) -> int:
    """Build + promote a gallery model version with the given threshold; return its id."""
    gallery_dir = "v1"
    _write_gallery_npz(os.path.join(store.models_root, gallery_dir, "gallery.npz"))
    version_id = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=2,
        threshold=threshold,
        quality="gallery",
        metrics=None,
        gallery_dir=gallery_dir,
    )
    store.promote_model(version_id)
    return version_id


def _one_event(store: Store):
    """Add three close motion frames -> one event; return (event_dict, frame_ids)."""
    base = 1_700_000_000_000
    f1 = store.add(_frame(1, base, motion=True, area=0.1), recv_ts_ms=base)
    f2 = store.add(_frame(2, base + 100, motion=True, area=0.9), recv_ts_ms=base + 100)
    f3 = store.add(_frame(3, base + 200, motion=True, area=0.3), recv_ts_ms=base + 200)
    events = store.events(None, None)["events"]
    assert len(events) == 1
    return events[0], (f1, f2, f3)


def _write_crop(store: Store, cat_id: int, frame_id: int, quality: str = "gallery",
                *, ts: int, color: int = 0, write_file: bool = True) -> str:
    """Add a live frame + an 'identified' dataset_items row with a real crop file.

    Returns the crop's relative path. ``write_file=False`` records the row but
    leaves NO file on disk (to exercise the isfile-guard fall-through).
    """
    # store.add assigns its OWN row id (ignoring the meta id); dataset_items must
    # anchor to that real id so add_dataset_items can resolve src_recv_ts.
    row_id = store.add(_frame(frame_id, ts), recv_ts_ms=ts)
    rel = os.path.join(f"cat_{cat_id}", f"{row_id}.jpg")
    if write_file:
        dest = os.path.join(store.dataset_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(_real_jpeg(color))
    n = store.add_dataset_items(
        [
            {
                "frame_id": row_id,
                "label_kind": "identified",
                "cat_id": cat_id,
                "quality": quality,
                "bbox": [0, 0, 8, 8],
                "crop_path": rel,
            }
        ]
    )
    assert n == 1
    return rel


@pytest.fixture
def api(tmp_path):
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


# --- Store.cats_overview last-seen -----------------------------------------


def test_cats_overview_uncalibrated_model_last_seen_none(tmp_path):
    # An uncalibrated (threshold None) model resolves every event to "unknown", so
    # no cat is ever "seen" — the fail-safe carries over from events().
    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)
    event, (_f1, f2, _f3) = _one_event(store)
    version = _promote_model(store, threshold=None)
    store.write_identifications_batch([(f2, version, cat["id"], 0.1, [0, 0, 8, 8])])

    rows = {c["id"]: c for c in store.cats_overview()}
    assert rows[cat["id"]]["last_seen_ts"] is None
    assert rows[cat["id"]]["last_seen_frame_id"] is None


def test_cats_overview_calibrated_below_threshold_sets_last_seen(tmp_path):
    # A calibrated model with a below-threshold identification names the cat; its
    # last-seen must match the SAME event the Activity feed shows.
    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)
    event, (_f1, f2, _f3) = _one_event(store)
    version = _promote_model(store, threshold=0.5)
    store.write_identifications_batch([(f2, version, cat["id"], 0.1, [0, 0, 8, 8])])

    rows = {c["id"]: c for c in store.cats_overview()}
    row = rows[cat["id"]]
    assert row["last_seen_ts"] == event["start_ts"]
    assert row["last_seen_frame_id"] == event["rep_frame_id"]
    # Consistency with the feed the Activity view renders.
    feed = store.events(None, None)["events"]
    assert feed[0]["identity"]["cat_id"] == cat["id"]


def test_cats_overview_no_model_last_seen_none(tmp_path):
    # No active model at all: identity is None on every event, so no last-seen.
    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)
    _one_event(store)

    rows = {c["id"]: c for c in store.cats_overview()}
    assert rows[cat["id"]]["last_seen_ts"] is None
    assert rows[cat["id"]]["last_seen_frame_id"] is None


def test_cats_overview_has_crop_flag(tmp_path):
    store = _store(tmp_path)
    with_crop = store.create_cat("Mittens", is_resident=True)
    without = store.create_cat("Shadow", is_resident=True)
    _write_crop(store, with_crop["id"], frame_id=100, ts=100_000)

    rows = {c["id"]: c for c in store.cats_overview()}
    assert rows[with_crop["id"]]["has_crop"] is True
    assert rows[without["id"]]["has_crop"] is False


# --- GET /api/cats/overview shape ------------------------------------------


def test_api_overview_no_model(api):
    client, store = api
    store.create_cat("Mittens", is_resident=True)
    body = client.get("/api/cats/overview").json()
    assert body["has_model"] is False
    assert body["uncalibrated"] is False
    assert len(body["cats"]) == 1
    cat = body["cats"][0]
    # No crop, no uploaded file -> has_avatar False; keys present.
    assert cat["has_avatar"] is False
    assert cat["has_crop"] is False
    assert cat["last_seen_ts"] is None


def test_api_overview_uncalibrated_flag(api):
    client, store = api
    store.create_cat("Mittens", is_resident=True)
    _promote_model(store, threshold=None)
    body = client.get("/api/cats/overview").json()
    assert body["has_model"] is True
    assert body["uncalibrated"] is True


def test_api_overview_calibrated_flag(api):
    client, store = api
    store.create_cat("Mittens", is_resident=True)
    _promote_model(store, threshold=0.5)
    body = client.get("/api/cats/overview").json()
    assert body["has_model"] is True
    assert body["uncalibrated"] is False


def test_api_overview_has_avatar_from_crop_and_upload(api):
    client, store = api
    crop_cat = store.create_cat("Mittens", is_resident=True)
    upload_cat = store.create_cat("Shadow", is_resident=True)
    bare_cat = store.create_cat("Ghost", is_resident=False)
    _write_crop(store, crop_cat["id"], frame_id=100, ts=100_000)
    # Upload an avatar for the second cat.
    assert client.post(f"/api/cats/{upload_cat['id']}/avatar", content=_real_jpeg(200)).status_code == 200

    cats = {c["id"]: c for c in client.get("/api/cats/overview").json()["cats"]}
    assert cats[crop_cat["id"]]["has_avatar"] is True  # via crop file
    assert cats[upload_cat["id"]]["has_avatar"] is True  # via uploaded file
    assert cats[bare_cat["id"]]["has_avatar"] is False

    # avatar_version = mtime (ms) of the file GET .../avatar would serve; the client
    # stamps it on the URL so a re-uploaded photo changes URL (busts cache) while an
    # unchanged one stays cacheable. Present for crop + upload, None for the bare cat.
    assert cats[crop_cat["id"]]["avatar_version"] == int(
        os.path.getmtime(store.cat_avatar_crop_path(crop_cat["id"])) * 1000
    )
    assert cats[upload_cat["id"]]["avatar_version"] == int(
        os.path.getmtime(store.avatar_path(upload_cat["id"])) * 1000
    )
    assert cats[bare_cat["id"]]["avatar_version"] is None


# --- POST /api/cats/{id}/avatar ---------------------------------------------


def test_post_avatar_happy_roundtrip(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    resp = client.post(f"/api/cats/{cat['id']}/avatar", content=_real_jpeg(120))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # The file now exists and GET serves it as a JPEG.
    assert os.path.isfile(store.avatar_path(cat["id"]))
    get = client.get(f"/api/cats/{cat['id']}/avatar")
    assert get.status_code == 200
    assert get.headers["content-type"] == "image/jpeg"


def test_post_avatar_non_image_is_400(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    resp = client.post(f"/api/cats/{cat['id']}/avatar", content=b"not an image at all")
    assert resp.status_code == 400
    assert not os.path.isfile(store.avatar_path(cat["id"]))


def test_post_avatar_unknown_cat_is_404(api):
    client, _store = api
    resp = client.post("/api/cats/999/avatar", content=_real_jpeg())
    assert resp.status_code == 404


def test_post_avatar_too_large_is_413(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    big = b"\x00" * (10 * 1024 * 1024 + 1)
    resp = client.post(f"/api/cats/{cat['id']}/avatar", content=big)
    assert resp.status_code == 413
    assert not os.path.isfile(store.avatar_path(cat["id"]))


# --- GET /api/cats/{id}/avatar precedence & fall-through --------------------


def test_get_avatar_uploaded_wins_over_crop(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    _write_crop(store, cat["id"], frame_id=100, ts=100_000, color=10)
    client.post(f"/api/cats/{cat['id']}/avatar", content=_real_jpeg(240))

    uploaded = client.get(f"/api/cats/{cat['id']}/avatar")
    assert uploaded.status_code == 200
    # Remove the override; GET now falls back to the crop -> different bytes.
    assert client.delete(f"/api/cats/{cat['id']}/avatar").json()["deleted"] is True
    crop = client.get(f"/api/cats/{cat['id']}/avatar")
    assert crop.status_code == 200
    assert crop.content != uploaded.content


def test_get_avatar_none_is_404(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    assert client.get(f"/api/cats/{cat['id']}/avatar").status_code == 404


def test_get_avatar_crop_row_but_file_deleted_falls_through(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    # A dataset_items row whose crop file was never written: isfile-guard skips it,
    # no uploaded file either -> 404, never a 500.
    _write_crop(store, cat["id"], frame_id=100, ts=100_000, write_file=False)
    assert client.get(f"/api/cats/{cat['id']}/avatar").status_code == 404


# --- DELETE /api/cats/{id}/avatar -------------------------------------------


def test_delete_avatar_removes_override_and_is_idempotent(api):
    client, store = api
    cat = store.create_cat("Mittens", is_resident=True)
    client.post(f"/api/cats/{cat['id']}/avatar", content=_real_jpeg(50))

    first = client.delete(f"/api/cats/{cat['id']}/avatar")
    assert first.status_code == 200
    assert first.json() == {"ok": True, "deleted": True}
    assert not os.path.isfile(store.avatar_path(cat["id"]))
    # GET now 404 (no crop fallback for this cat).
    assert client.get(f"/api/cats/{cat['id']}/avatar").status_code == 404
    # Second DELETE is still 200, deleted False.
    second = client.delete(f"/api/cats/{cat['id']}/avatar")
    assert second.status_code == 200
    assert second.json() == {"ok": True, "deleted": False}

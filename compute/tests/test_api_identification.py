"""Tests for the gallery-build/promote/identify routes on ``compute/api/app.py``
(the identification-gallery-activity spec's API layer).

Mirrors ``test_api_analysis.py``'s / ``test_annotation.py``'s
``create_app(store=..., start_collector=False)`` ``TestClient`` pattern: a real
temp ``Store`` (these routes are thin validation + store/manager wiring, not a
real embedding pipeline) plus a hand-rolled ``FakeTrainingManager`` injected via
``training_manager=`` so no queue/thread/torch is involved — only the routing,
the 400/409/404 mapping, and the store-boundary calls the endpoints make are
under test here (``test_training_runner.py`` already covers the real manager's
queue/threading/dispatch with fake probe/gallery/identify callables, and
``compute/collection/store.py``'s model-version methods have no dedicated test
file yet, so the promote/models-shape assertions double as light coverage of
those too).

``Embedder.ensure_available`` is monkeypatched to a no-op so the gallery-build
and identify "happy enqueue" paths never touch torch/torchvision/cv2 — these
tests run anywhere, matching ``compute/CLAUDE.md``'s "GPU-/model-dependent tests
should run against small fixtures or be skippable" rule.
"""
from __future__ import annotations

import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal but genuinely valid JPEG (SOI ... EOI); these tests never decode it —
# the crop files referenced by dataset_items rows are never opened here, since
# the fake training manager never actually embeds anything.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int = 1, ts: int = 1_000) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=False, bbox=None, area=0.0)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _label(store: Store, cat_id: int, frame_id: int, quality: str = "gallery") -> None:
    """Add a live frame then an ``identified`` dataset_items row anchored to it.

    Mirrors ``test_feasibility_runs.py``'s helper: ``add_dataset_items`` resolves
    ``src_recv_ts`` from a live frame, so each labelled crop needs one added first.
    The ``crop_path`` is a plain string (never a real file) — fine here since
    ``count_identified_crops``/the fake manager never open it.
    """
    row_id = store.add(_frame(frame_id=frame_id, ts=frame_id * 1000), recv_ts_ms=frame_id * 1000)
    n = store.add_dataset_items(
        [
            {
                "frame_id": row_id,
                "label_kind": "identified",
                "cat_id": cat_id,
                "quality": quality,
                "bbox": [0, 0, 10, 10],
                "crop_path": f"cat_{cat_id}/{frame_id}.jpg",
            }
        ]
    )
    assert n == 1


def _write_gallery_npz(path: str) -> None:
    """Write a tiny-but-genuine ``gallery.npz`` so ``promote_model``'s on-disk
    artifact check (``os.path.isfile``) passes without a real embedding run."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez(
        path,
        vectors=np.zeros((2, 4), dtype=np.float32),
        cat_ids=np.array([1, 2], dtype=np.int64),
        backbone="dinov2_vits14",
        imgsz=224,
    )


class FakeTrainingManager:
    """A minimal stand-in for ``TrainingManager``: records calls, returns a canned result.

    The routes under test only ever call ``enqueue_gallery_build`` /
    ``enqueue_identify`` on the injected manager (see ``compute/api/app.py``), so
    this fake implements exactly those two — recording each call's args for the
    forwarding assertions below and returning a fixed ``{"position", "deduped"}``
    the route folds ``"enough": True`` onto. Built standalone (not a real-manager
    subclass, unlike ``test_api_analysis.py``'s ``SpyAnalysisManager``) since no
    test here needs a job to actually run — ``test_training_runner.py`` already
    covers the real queue/threading/dispatch behavior.
    """

    def __init__(self) -> None:
        self.gallery_build_calls: "list[dict]" = []
        self.identify_calls: "list[dict]" = []

    def enqueue_gallery_build(self, store, qualities) -> dict:
        self.gallery_build_calls.append({"store": store, "qualities": qualities})
        return {"position": 0, "deduped": False}

    def enqueue_identify(self, store, since_id, until_id) -> dict:
        self.identify_calls.append({"store": store, "since_id": since_id, "until_id": until_id})
        return {"position": 0, "deduped": False}


class _FakeClient:
    """A stand-in edge connection: no network, no real Pi (mirrors test_api_analysis.py)."""

    def iter_stream_reconnecting(self):
        return iter(())


@pytest.fixture
def make_app(tmp_path):
    """Factory for a ``TestClient`` over a fresh ``Store`` + ``FakeTrainingManager``.

    Mirrors ``test_api_analysis.py``'s ``make_app`` fixture (an explicit ``Store``,
    ``start_collector=False`` so no real edge/thread is created), injecting the
    fake manager above instead of a real ``TrainingManager`` so no queue/thread is
    involved. Returns ``(client, store, manager)``.
    """

    def _make():
        from compute.api.app import create_app

        store = _store(tmp_path)
        manager = FakeTrainingManager()
        app = create_app(
            store=store, client=_FakeClient(), start_collector=False, training_manager=manager
        )
        return TestClient(app), store, manager

    return _make


# --- POST /api/training/gallery/build -------------------------------------------


def test_gallery_build_bad_quality_is_400(make_app):
    client, _store, manager = make_app()
    resp = client.post("/api/training/gallery/build", json={"qualities": ["bogus"]})
    assert resp.status_code == 400
    assert manager.gallery_build_calls == []  # rejected before ever reaching the manager


def test_gallery_build_cold_start_returns_enough_false(make_app):
    # No labelled crops at all: the pre-check short-circuits with a 200 friendly
    # empty-state, BEFORE Embedder.ensure_available (so no torch is touched) and
    # WITHOUT enqueuing anything.
    client, _store, manager = make_app()
    resp = client.post("/api/training/gallery/build", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enough"] is False
    assert body["n_crops"] == 0
    assert body["n_cats"] == 0
    assert "message" in body and body["message"]
    assert manager.gallery_build_calls == []


def test_gallery_build_happy_enqueues(make_app, monkeypatch):
    # Two cats, one crop each -> enough data. Stub the heavy-dep gate so the
    # request never imports torch, then assert the enqueue reached the fake
    # manager with the qualities forwarded, and the response merges enough=True
    # onto whatever the manager returned.
    monkeypatch.setattr("compute.identification.embed.Embedder.ensure_available", lambda self: None)
    client, store, manager = make_app()
    _label(store, cat_id=1, frame_id=1)
    _label(store, cat_id=2, frame_id=2)

    resp = client.post("/api/training/gallery/build", json={"qualities": ["gallery"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enough"] is True
    assert body["position"] == 0
    assert body["deduped"] is False
    assert manager.gallery_build_calls == [{"store": store, "qualities": ["gallery"]}]


def test_gallery_build_null_qualities_forwards_none(make_app, monkeypatch):
    # Absent/null qualities means "all grades" -> forwarded as None at the
    # store/manager boundary, not an empty list.
    monkeypatch.setattr("compute.identification.embed.Embedder.ensure_available", lambda self: None)
    client, store, manager = make_app()
    _label(store, cat_id=1, frame_id=1)
    _label(store, cat_id=2, frame_id=2)

    resp = client.post("/api/training/gallery/build", json={})
    assert resp.status_code == 200
    assert resp.json()["enough"] is True
    assert manager.gallery_build_calls == [{"store": store, "qualities": None}]


# --- GET /api/training/models ----------------------------------------------------


def test_training_models_empty_shape(make_app):
    client, _store, _manager = make_app()
    resp = client.get("/api/training/models")
    assert resp.status_code == 200
    assert resp.json() == {"models": [], "active": None}


def test_training_models_lists_a_built_draft(make_app):
    # A drafted (not yet promoted) version lists with gallery_available False,
    # since no gallery.npz was ever written for it; active stays None.
    client, store, _manager = make_app()
    version_id = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=5,
        threshold=0.3,
        quality="gallery",
        metrics={"per_cat": []},
        gallery_dir="not-on-disk",
    )

    resp = client.get("/api/training/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is None
    assert len(body["models"]) == 1
    model = body["models"][0]
    assert model["id"] == version_id
    assert model["status"] == "draft"
    assert model["quality"] == "gallery"
    assert model["threshold"] == 0.3
    assert model["gallery_available"] is False  # gallery.npz was never written


# --- POST /api/training/models/{id}/promote -------------------------------------


def test_promote_unknown_model_is_404(make_app):
    client, _store, _manager = make_app()
    resp = client.post("/api/training/models/999/promote")
    assert resp.status_code == 404


def test_promote_happy_flips_status_and_appears_as_active(make_app):
    client, store, _manager = make_app()
    gallery_dir = "v1"
    _write_gallery_npz(os.path.join(store.models_root, gallery_dir, "gallery.npz"))
    version_id = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=2,
        threshold=0.3,
        quality="gallery",
        metrics=None,
        gallery_dir=gallery_dir,
    )

    resp = client.post(f"/api/training/models/{version_id}/promote")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == version_id
    assert body["status"] == "active"

    models = client.get("/api/training/models").json()
    assert models["active"]["id"] == version_id
    assert models["active"]["status"] == "active"
    assert any(m["id"] == version_id and m["status"] == "active" for m in models["models"])


# --- POST /api/identify/run -------------------------------------------------------


def test_identify_run_no_active_model_is_409(make_app):
    client, _store, manager = make_app()
    resp = client.post("/api/identify/run", json={})
    assert resp.status_code == 409
    assert manager.identify_calls == []


def test_identify_run_happy_enqueues(make_app, monkeypatch):
    monkeypatch.setattr("compute.identification.embed.Embedder.ensure_available", lambda self: None)
    client, store, manager = make_app()
    gallery_dir = "v1"
    _write_gallery_npz(os.path.join(store.models_root, gallery_dir, "gallery.npz"))
    version_id = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=2,
        threshold=0.3,
        quality="gallery",
        metrics=None,
        gallery_dir=gallery_dir,
    )
    store.promote_model(version_id)  # make it the active model the endpoint requires

    resp = client.post("/api/identify/run", json={"since_id": 1, "until_id": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enough"] is True
    assert body["position"] == 0
    assert body["deduped"] is False
    assert manager.identify_calls == [{"store": store, "since_id": 1, "until_id": 5}]


def test_identify_run_inverted_range_is_400(make_app, monkeypatch):
    # Active model present, but since_id > until_id is an impossible window —
    # rejected before ever reaching the manager, same guard every windowed run uses.
    monkeypatch.setattr("compute.identification.embed.Embedder.ensure_available", lambda self: None)
    client, store, manager = make_app()
    gallery_dir = "v1"
    _write_gallery_npz(os.path.join(store.models_root, gallery_dir, "gallery.npz"))
    version_id = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=2,
        threshold=None,
        quality="gallery",
        metrics=None,
        gallery_dir=gallery_dir,
    )
    store.promote_model(version_id)

    resp = client.post("/api/identify/run", json={"since_id": 90, "until_id": 10})
    assert resp.status_code == 400
    assert manager.identify_calls == []

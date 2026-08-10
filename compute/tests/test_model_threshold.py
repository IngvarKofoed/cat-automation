"""Tests for ``POST /api/training/models/{id}/threshold`` (open-set-scoring-and-
calibration spec's "Threshold as a setting" — stream 2).

``Store.set_model_threshold`` already exists and owns the real contract (the
``[0, 2]`` range check, the ``metrics.threshold_built`` copy-on-first-override,
the ``metrics.threshold_source_run_id`` stamp) — this file does not re-test that
store method's internals, it tests that the ROUTE reaches it correctly: the
request-model shape (required-but-nullable, ``Field``-bounded, boolean-rejecting
— mirroring ``LightingThresholdRequest``, per ``test_lighting.py``), and the
ValueError-to-HTTP-status mapping (404 for an unknown model id, 400 for anything
else — the same split ``/promote`` already uses for 404 vs its own alternate
code).

Mirrors ``test_location.py``'s lightweight ``create_app(store=..., client=None,
start_collector=False)`` + ``TestClient`` pattern — no training manager is
needed since this route never touches one.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _make_app(tmp_path) -> "tuple[TestClient, Store]":
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, client=None, start_collector=False)
    return TestClient(app), store


def _add_version(store: Store, threshold: "float | None" = 0.3, metrics: "dict | None" = None) -> int:
    """A minimal ``model_versions`` row to set a threshold on."""
    return store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=5,
        threshold=threshold,
        quality="gallery",
        metrics=metrics,
        gallery_dir="not-on-disk",
    )


# --- The request model itself (mirrors test_lighting.py's direct pydantic checks) --


def test_request_model_requires_an_explicit_threshold():
    """``{}`` must 422, not silently coerce to a default — clearing/setting is
    always an explicit act (mirrors ``LightingThresholdRequest``)."""
    from pydantic import ValidationError

    from compute.api.app import ModelThresholdRequest

    with pytest.raises(ValidationError):
        ModelThresholdRequest()
    assert ModelThresholdRequest(threshold=None).threshold is None
    assert ModelThresholdRequest(threshold=0.5).threshold == pytest.approx(0.5)


def test_request_model_rejects_boolean_threshold_and_run_id():
    # pydantic treats bool as an int subtype: without the guard `true` would coerce
    # to a 1.0 threshold, and `true` for source_run_id would coerce to run id 1.
    from pydantic import ValidationError

    from compute.api.app import ModelThresholdRequest

    with pytest.raises(ValidationError):
        ModelThresholdRequest(threshold=True)
    with pytest.raises(ValidationError):
        ModelThresholdRequest(threshold=0.5, source_run_id=True)


# --- POST /api/training/models/{id}/threshold: happy paths --------------------


def test_set_threshold_happy_path_updates_and_round_trips(tmp_path):
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=0.3)

    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": 0.55})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == version_id
    assert body["threshold"] == pytest.approx(0.55)

    # Persisted, not just echoed — a fresh read must see it too.
    models = client.get("/api/training/models").json()["models"]
    row = next(m for m in models if m["id"] == version_id)
    assert row["threshold"] == pytest.approx(0.55)


def test_set_threshold_null_restores_uncalibrated_fail_safe(tmp_path):
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=0.4)

    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": None})
    assert resp.status_code == 200
    body = resp.json()
    # Uncalibrated must be `None`, never coerced to 0 — 0 is a real (extreme)
    # threshold, not the "names nobody" fail-safe.
    assert body["threshold"] is None

    models = client.get("/api/training/models").json()["models"]
    row = next(m for m in models if m["id"] == version_id)
    assert row["threshold"] is None


def test_set_threshold_boundary_values_are_accepted(tmp_path):
    # 0.0 and 2.0 are INCLUSIVE bounds (cosine distance's full range), not an
    # off-by-one exclusion.
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=None)

    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": 0.0})
    assert resp.status_code == 200
    assert resp.json()["threshold"] == pytest.approx(0.0)

    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": 2.0})
    assert resp.status_code == 200
    assert resp.json()["threshold"] == pytest.approx(2.0)


def test_set_threshold_forwards_source_run_id(tmp_path):
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=0.3)

    resp = client.post(
        f"/api/training/models/{version_id}/threshold",
        json={"threshold": 0.42, "source_run_id": 7},
    )
    assert resp.status_code == 200
    assert resp.json()["metrics"]["threshold_source_run_id"] == 7


# --- Out-of-range rejection, both ends -----------------------------------------


def test_set_threshold_above_range_is_422(tmp_path):
    # A mistyped 4.36 for 0.436 is exactly the failure this bound exists to catch
    # (see the spec) — must be rejected at the pydantic boundary, never reach the store.
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=0.3)

    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": 4.36})
    assert resp.status_code == 422

    # Rejected before the store was ever touched — the original value survives.
    models = client.get("/api/training/models").json()["models"]
    row = next(m for m in models if m["id"] == version_id)
    assert row["threshold"] == pytest.approx(0.3)


def test_set_threshold_below_range_is_422(tmp_path):
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=0.3)

    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": -0.1})
    assert resp.status_code == 422

    models = client.get("/api/training/models").json()["models"]
    row = next(m for m in models if m["id"] == version_id)
    assert row["threshold"] == pytest.approx(0.3)


# --- Unknown model id -----------------------------------------------------------


def test_set_threshold_unknown_model_is_404(tmp_path):
    client, _store = _make_app(tmp_path)

    resp = client.post("/api/training/models/999/threshold", json={"threshold": 0.5})
    assert resp.status_code == 404


# --- threshold_built: stamped once, never overwritten --------------------------


def test_threshold_built_is_stamped_on_first_override_only(tmp_path):
    """The build's own value survives every later override — see the store's
    docstring. This is the route's most important honesty guarantee: without it,
    a second edit destroys the record of what the gallery was actually built with,
    and BUILT/EFFECTIVE could no longer be shown side by side as two different
    numbers."""
    client, store = _make_app(tmp_path)
    version_id = _add_version(store, threshold=0.3, metrics=None)

    # First override: no `threshold_built` exists yet, so it is stamped from the
    # PRE-override value (0.3), not the new one (0.55).
    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": 0.55})
    assert resp.status_code == 200
    assert resp.json()["metrics"]["threshold_built"] == pytest.approx(0.3)

    # Second override: threshold_built must stay 0.3, NOT jump to 0.55.
    resp = client.post(f"/api/training/models/{version_id}/threshold", json={"threshold": 0.9})
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == pytest.approx(0.9)
    assert body["metrics"]["threshold_built"] == pytest.approx(0.3)

    # And it survives a read-back too, not just the write response.
    models = client.get("/api/training/models").json()["models"]
    row = next(m for m in models if m["id"] == version_id)
    assert row["metrics"]["threshold_built"] == pytest.approx(0.3)

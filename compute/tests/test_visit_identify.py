"""Tests for the per-visit analyse + identify job and its route.

See docs/specs/2026-08-01-unanalysed-visits-analyse-identify.md. Two layers:

- The ``visit-identify`` job kind on ``compute/learning/runner.TrainingManager`` —
  the detect-then-identify pair over ONE visit span. Fakes are injected for BOTH
  halves (``detector=`` / ``analyzer_factory=`` alongside the existing
  ``identifier=``), so the whole thing runs with NO torch and no real model,
  mirroring ``test_training_jobs.py``'s style.
- ``POST /api/identify/visit`` — the validation + wiring layer, over the
  ``create_app(store=..., training_manager=...)`` ``TestClient`` pattern
  ``test_api_identification.py`` established.

The behaviours worth locking, all of which differ from the whole-store
``identify`` job: no active model is a SUCCESS (detect still ran), a cancel during
detect must not identify a half-detected span, and the span bounds are mandatory
and width-capped because the household's phone calls this route.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.identification.embed import EmbedCancelled
from compute.learning.runner import TrainingManager

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _wait(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses; return its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _make_active_model(store: Store) -> dict:
    """Insert + promote a model_versions row with a real gallery.npz on disk.

    Copied from ``test_training_jobs.py`` — set-up for the identify half, not a thing
    under test.
    """
    gallery_dir = "v1"
    d = os.path.join(store.models_root, gallery_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gallery.npz"), "wb") as fh:
        fh.write(b"stub-gallery")
    store.add_model_version(
        status="active", kind="gallery", backbone="dinov2_vits14", imgsz=224,
        n_cats=2, n_vectors=4, threshold=0.5, quality="gallery", metrics=None,
        gallery_dir=gallery_dir,
    )
    model = store.active_model()
    assert model is not None, "test setup: active_model() should resolve the row just inserted"
    return model


class _SpyDetector:
    """Fake ``run_analysis``: records the (analyzer, span, flags) it was called with."""

    def __init__(self) -> None:
        self.calls: "list[dict]" = []

    def __call__(self, store, analyzer, manager, reanalyze=False, since_id=None,
                 until_id=None, motion_only=False):
        self.calls.append({
            "analyzer": analyzer, "since_id": since_id, "until_id": until_id,
            "reanalyze": reanalyze, "motion_only": motion_only,
            "stop_event": manager.stop_event,
        })


class _CancelDuringDetect:
    """Fake detector that trips the manager's stop_event and returns NORMALLY.

    That is precisely what the real ``run_analysis`` does when a cancel lands: it
    breaks between batches and returns, leaving the span PARTLY detected — it does
    not raise. The job must notice and skip the identify half.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, store, analyzer, manager, reanalyze=False, since_id=None,
                 until_id=None, motion_only=False):
        self.calls += 1
        manager.stop_event.set()


class _SpyIdentifier:
    """Fake ``run_identify``: records args, reports two progress ticks."""

    def __init__(self, n_identified: int = 3) -> None:
        self.calls = 0
        self.received: "list[tuple]" = []
        self.n_identified = n_identified

    def __call__(self, store, model, gallery_path, since_id, until_id, progress=None):
        self.calls += 1
        self.received.append((model["id"], gallery_path, since_id, until_id))
        if progress is not None:
            progress(0, self.n_identified)
            progress(self.n_identified, self.n_identified)
        return {"n_identified": self.n_identified}


class _GatedDetector:
    """Blocks inside detect until released — freezes the head job for dedup assertions."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, store, analyzer, manager, **kw):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)


def _manager(detector, identifier=None) -> TrainingManager:
    """A TrainingManager with both GPU halves faked out.

    ``analyzer_factory`` returns a sentinel string rather than resolving the real
    ``yolo-serial`` analyzer — the factory exists precisely so nothing imports torch here.
    """
    return TrainingManager(
        detector=detector,
        identifier=identifier if identifier is not None else _SpyIdentifier(),
        analyzer_factory=(lambda: "fake-yolo-serial"),
    )


# --- the job -----------------------------------------------------------------


def test_visit_identify_detects_then_identifies_the_same_span(tmp_path):
    store = _store(tmp_path)
    model = _make_active_model(store)
    detector, identifier = _SpyDetector(), _SpyIdentifier()
    mgr = _manager(detector, identifier)

    mgr.enqueue_visit_identify(store, 100, 140)
    assert _wait(lambda: not mgr.running and mgr.status()["result"] is not None)

    # Detect ran over exactly the visit span, filling MISSING verdicts (not reanalyze)
    # and NOT motion_only — a cat pausing at the flap leaves calm motion=0 frames that
    # identify best, so the span-scoped sweep must cover them.
    assert len(detector.calls) == 1
    call = detector.calls[0]
    assert (call["since_id"], call["until_id"]) == (100, 140)
    assert call["reanalyze"] is False and call["motion_only"] is False
    assert call["analyzer"] == "fake-yolo-serial"

    # ...then identify, over the same span and the active model's gallery.
    assert identifier.received == [(model["id"], model["gallery_path"], 100, 140)]

    result = mgr.status()["result"]
    assert result["kind"] == "visit-identify"
    assert result["identified"] is True
    assert result["n_identified"] == 3
    assert (result["since_id"], result["until_id"]) == (100, 140)
    assert mgr.status()["history"][0]["state"] == "done"


def test_visit_identify_with_no_active_model_detects_and_succeeds(tmp_path):
    # The divergence from /api/identify/run's 409: detect is the half that resolves the
    # `unanalyzed` subject and is useful with no gallery at all, so this is a SUCCESS
    # with identified=False — not a failure. Pre-gallery is the normal state in Phase 1.
    store = _store(tmp_path)
    detector, identifier = _SpyDetector(), _SpyIdentifier()
    mgr = _manager(detector, identifier)

    mgr.enqueue_visit_identify(store, 5, 9)
    assert _wait(lambda: not mgr.running and mgr.status()["result"] is not None)

    assert len(detector.calls) == 1        # detect still ran
    assert identifier.calls == 0           # nothing to identify against
    result = mgr.status()["result"]
    assert result["identified"] is False and result["n_identified"] == 0
    assert mgr.status()["history"][0]["state"] == "done"
    assert mgr.status()["error"] is None


def test_cancel_during_detect_skips_identify_and_records_canceled(tmp_path):
    # A stop during detect returns normally with the span only PARTLY detected. Naming it
    # now would report a whole-visit count for a fraction of it, so the job must bail —
    # and it must record 'canceled', which means returning NO summary (the manager checks
    # result_summary before stop_event, so any summary would read as 'done').
    store = _store(tmp_path)
    _make_active_model(store)
    detector, identifier = _CancelDuringDetect(), _SpyIdentifier()
    mgr = _manager(detector, identifier)

    mgr.enqueue_visit_identify(store, 1, 50)
    assert _wait(lambda: not mgr.running and mgr.status()["history"])

    assert detector.calls == 1
    assert identifier.calls == 0
    assert mgr.status()["history"][0]["state"] == "canceled"
    assert mgr.status()["result"] is None
    assert mgr.status()["error"] is None      # a cancel is not a fault


def test_cancel_during_identify_yields_canceled(tmp_path):
    # The other cancel path: detect completed, the embed loop honoured the stop signal and
    # raised EmbedCancelled. Handled by _run's existing finally, same as `identify`.
    store = _store(tmp_path)
    _make_active_model(store)

    entered = threading.Event()

    def _cancel_identifier(store_, model, gallery_path, since_id, until_id, progress=None):
        entered.set()
        while True:
            if progress is not None and not progress(0, 100):
                raise EmbedCancelled()
            time.sleep(0.005)

    mgr = _manager(_SpyDetector(), _cancel_identifier)
    mgr.enqueue_visit_identify(store, 1, 50)
    assert entered.wait(timeout=5)
    mgr.cancel()

    assert _wait(lambda: not mgr.running and mgr.status()["history"])
    assert mgr.status()["history"][0]["state"] == "canceled"
    assert mgr.status()["error"] is None


def test_double_tap_on_the_running_visit_is_deduped(tmp_path):
    # The button's double-click guard: the same span, while running, collapses.
    store = _store(tmp_path)
    detector = _GatedDetector()
    mgr = _manager(detector)

    first = mgr.enqueue_visit_identify(store, 10, 20)
    assert first["position"] == 0 and first["deduped"] is False
    assert detector.entered.wait(timeout=5)

    again = mgr.enqueue_visit_identify(store, 10, 20)
    assert again["deduped"] is True and again["position"] == 0

    # A DIFFERENT visit is different work and queues behind it.
    other = mgr.enqueue_visit_identify(store, 30, 40)
    assert other["deduped"] is False and other["position"] == 1

    detector.release.set()
    assert _wait(lambda: not mgr.running)
    assert detector.calls == 2  # the deduped tap never ran; the other visit did


def test_analyzer_is_built_once_and_reused_across_visits(tmp_path):
    # The factory is called lazily (never at import) and only once per process — weights
    # load once, and run_analysis.prepare() is idempotent thereafter.
    store = _store(tmp_path)
    built = []

    def _factory():
        built.append(1)
        return "fake-yolo-serial"

    mgr = TrainingManager(
        detector=_SpyDetector(), identifier=_SpyIdentifier(), analyzer_factory=_factory
    )
    assert built == []  # not built at construction

    mgr.enqueue_visit_identify(store, 1, 5)
    assert _wait(lambda: not mgr.running and mgr.status()["result"] is not None)
    mgr.enqueue_visit_identify(store, 6, 9)
    assert _wait(lambda: not mgr.running and mgr.status()["result"]["since_id"] == 6)
    assert built == [1]


# --- POST /api/identify/visit ------------------------------------------------


class _FakeTrainingManager:
    """Records ``enqueue_visit_identify`` calls; returns a canned enqueue snapshot."""

    def __init__(self) -> None:
        self.calls: "list[tuple]" = []

    def enqueue_visit_identify(self, store, start_id, end_id) -> dict:
        self.calls.append((start_id, end_id))
        return {"position": 0, "deduped": False}


class _FakeClient:
    def iter_stream_reconnecting(self):
        return iter(())


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """``(client, store, manager)`` over a fresh Store + fake training manager.

    Both dep checks are monkeypatched to no-ops so the happy path never touches
    torch/ultralytics — matching compute/CLAUDE.md's "runnable anywhere" rule.
    """

    def _make():
        from compute.api.app import create_app
        import compute.api.app as app_module
        from compute.identification.embed import Embedder

        monkeypatch.setattr(Embedder, "ensure_available", lambda self: None)
        monkeypatch.setattr(
            app_module, "get_analyzer", lambda name: type("A", (), {"ensure_available": lambda s: None})()
        )
        store = _store(tmp_path)
        mgr = _FakeTrainingManager()
        app = create_app(
            store=store, client=_FakeClient(), start_collector=False, training_manager=mgr
        )
        return TestClient(app), store, mgr

    return _make


def test_visit_route_forwards_the_span(make_app):
    client, store, mgr = make_app()
    r = client.post("/api/identify/visit", json={"start_id": 100, "end_id": 140})
    assert r.status_code == 200
    assert mgr.calls == [(100, 140)]
    # No promoted gallery -> the UI is told naming will be skipped, up front.
    assert r.json()["will_identify"] is False


def test_visit_route_reports_will_identify_with_an_active_model(make_app):
    client, store, mgr = make_app()
    _make_active_model(store)
    r = client.post("/api/identify/visit", json={"start_id": 1, "end_id": 5})
    assert r.status_code == 200 and r.json()["will_identify"] is True


def test_visit_route_requires_both_bounds(make_app):
    # Every OTHER windowed run reads a missing bound as "unbounded"; here that would turn
    # a per-visit tap into a whole-store sweep, so presence is mandatory (422 from pydantic).
    client, store, mgr = make_app()
    assert client.post("/api/identify/visit", json={"start_id": 5}).status_code == 422
    assert client.post("/api/identify/visit", json={"end_id": 5}).status_code == 422
    assert client.post("/api/identify/visit", json={}).status_code == 422
    assert mgr.calls == []


def test_visit_route_rejects_booleans_and_non_positive_ids(make_app):
    # pydantic treats bool as an int subtype, so an unguarded `true` coerces to frame id 1
    # and enqueues a job over the start of the store. Mirrors FlagSpanRequest's guard.
    client, store, mgr = make_app()
    assert client.post("/api/identify/visit", json={"start_id": True, "end_id": 5}).status_code == 422
    assert client.post("/api/identify/visit", json={"start_id": 1, "end_id": False}).status_code == 422
    assert client.post("/api/identify/visit", json={"start_id": 0, "end_id": 5}).status_code == 422
    assert client.post("/api/identify/visit", json={"start_id": -3, "end_id": 5}).status_code == 422
    assert mgr.calls == []


def test_visit_route_rejects_an_inverted_span(make_app):
    client, store, mgr = make_app()
    r = client.post("/api/identify/visit", json={"start_id": 90, "end_id": 10})
    assert r.status_code == 400
    assert mgr.calls == []


def test_visit_route_rejects_a_span_wider_than_the_cap(make_app):
    from compute.api.app import _MAX_VISIT_SPAN

    client, store, mgr = make_app()
    r = client.post(
        "/api/identify/visit", json={"start_id": 1, "end_id": 1 + _MAX_VISIT_SPAN + 1}
    )
    assert r.status_code == 400
    assert "cap" in r.json()["detail"]
    assert mgr.calls == []
    # Exactly at the cap is fine — the guard is a ceiling, not an off-by-one fence.
    ok = client.post("/api/identify/visit", json={"start_id": 1, "end_id": 1 + _MAX_VISIT_SPAN})
    assert ok.status_code == 200


def test_visit_route_503s_when_the_detector_deps_are_missing(monkeypatch, tmp_path):
    # Detect ALWAYS runs, so its deps are always required — even with no gallery, where
    # the embedder's are not.
    from compute.api.app import create_app
    import compute.api.app as app_module

    def _boom(name):
        class _A:
            def ensure_available(self):
                raise ImportError("install compute/requirements-analysis.txt")
        return _A()

    monkeypatch.setattr(app_module, "get_analyzer", _boom)
    store = _store(tmp_path)
    mgr = _FakeTrainingManager()
    client = TestClient(
        create_app(store=store, client=_FakeClient(), start_collector=False, training_manager=mgr)
    )
    r = client.post("/api/identify/visit", json={"start_id": 1, "end_id": 5})
    assert r.status_code == 503
    assert mgr.calls == []

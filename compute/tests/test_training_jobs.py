"""Tests for the ``gallery-build`` and ``identify`` job kinds on
``compute/learning/runner.TrainingManager`` — the two kinds the
identification-gallery-activity spec adds alongside the existing ``feasibility``
kind (see ``test_training_runner.py``, which these mirror in style and fakes).

As with the feasibility tests, a deterministic FAKE ``gallery_builder`` /
``identifier`` stands in for the real embed→match→persist pipeline, so the whole
queue/threading/lifecycle is exercised with NO torch and no real model — the
fakes are injected via ``TrainingManager(gallery_builder=..., identifier=...)``.

``_GatedGalleryBuilder`` blocks inside the run (on a release ``Event``) so the
head job can be held provably-running while a second enqueue's dedup/position is
asserted, exactly like ``_GatedProbe``. ``_CancelIdentifier`` polls its
``progress`` callback and raises ``EmbedCancelled`` the instant it goes falsy —
what the real ``run_identify``/``embed_crops`` do when ``stop_event`` is set.

A real temp ``Store`` is used throughout, so the gallery-build success test can
assert an actual ``model_versions`` row is persisted, and the identify tests can
set up a genuine ``active_model()`` row for the job to resolve.
"""
from __future__ import annotations

import os
import threading
import time

from compute.collection.store import Store
from compute.identification.embed import EmbedCancelled
from compute.learning.runner import TrainingManager


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


def _write_gallery_file(out_dir: str) -> None:
    """Write a stub ``gallery.npz`` where the real builder would, so a subsequent
    ``list_model_versions()``/``active_model()`` read finds the artifact on disk."""
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "gallery.npz"), "wb") as fh:
        fh.write(b"stub-gallery")


def _make_active_model(store: Store, threshold: "float | None" = 0.5) -> dict:
    """Insert + promote a model_versions row with a real gallery.npz on disk.

    Bypasses the gallery-build job entirely (this is set-up for identify tests,
    not a thing under test) — inserted directly ``status='active'`` since only one
    row exists, so ``promote_model``'s retire-then-activate dance is unnecessary.
    """
    gallery_dir = "v1"
    _write_gallery_file(os.path.join(store.models_root, gallery_dir))
    store.add_model_version(
        status="active",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=4,
        threshold=threshold,
        quality="gallery",
        metrics=None,
        gallery_dir=gallery_dir,
    )
    model = store.active_model()
    assert model is not None, "test setup: active_model() should resolve the row just inserted"
    return model


class _SuccessGalleryBuilder:
    """Non-blocking fake: two progress ticks, a stub ``gallery.npz``, ``enough=True``.

    Records every ``out_dir`` it was called with (mirrors ``_SuccessProbe``) so a
    dedup/re-run test can prove distinct invocations happened.
    """

    def __init__(self, n_crops: int = 4, n_cats: int = 2, n_vectors: int = 4) -> None:
        self.calls = 0
        self.out_dirs: "list[str]" = []
        self.n_crops = n_crops
        self.n_cats = n_cats
        self.n_vectors = n_vectors

    def __call__(self, store, out_dir, qualities=None, progress=None):
        self.calls += 1
        self.out_dirs.append(out_dir)
        if progress is not None:
            progress(0, self.n_crops)
            progress(self.n_crops, self.n_crops)
        _write_gallery_file(out_dir)
        return {
            "enough": True,
            "n_crops": self.n_crops,
            "n_cats": self.n_cats,
            "n_vectors": self.n_vectors,
            "backbone": "dinov2_vits14",
            "imgsz": 224,
            "threshold": 0.42,
            "quality": "all" if qualities is None else "+".join(qualities),
            "metrics": {"per_cat": [], "backbone": "dinov2_vits14", "imgsz": 224,
                        "threshold_balanced_acc": 0.9},
            "out_dir": out_dir,
        }


class _GatedGalleryBuilder:
    """A fake that blocks inside the run until ``release`` is set — the head-job freezer.

    Mirrors ``_GatedProbe``: sets ``entered`` once executing (so a test can assert
    the job is provably running) then waits on ``release`` before finishing as a
    normal successful build.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, store, out_dir, qualities=None, progress=None):
        self.calls += 1
        if progress is not None:
            progress(0, 1)
        self.entered.set()
        self.release.wait(timeout=5)
        if progress is not None:
            progress(1, 1)
        _write_gallery_file(out_dir)
        return {
            "enough": True,
            "n_crops": 2,
            "n_cats": 2,
            "n_vectors": 2,
            "backbone": "dinov2_vits14",
            "imgsz": 224,
            "threshold": 0.3,
            "quality": "all" if qualities is None else "+".join(qualities),
            "metrics": {"per_cat": [], "backbone": "dinov2_vits14", "imgsz": 224,
                        "threshold_balanced_acc": 0.5},
            "out_dir": out_dir,
        }


class _SuccessIdentifier:
    """Non-blocking fake ``identifier``: records the args it was called with (proving
    the manager resolved the active model + its gallery path correctly) and reports
    two progress ticks before returning ``n_identified``."""

    def __init__(self, n_identified: int = 5) -> None:
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


class _CancelIdentifier:
    """A fake that loops on ``progress`` and raises ``EmbedCancelled`` when it goes falsy —
    exactly the real ``run_identify``/``embed_crops`` cancel contract (mirrors ``_CancelProbe``)."""

    def __init__(self) -> None:
        self.entered = threading.Event()

    def __call__(self, store, model, gallery_path, since_id, until_id, progress=None):
        self.entered.set()
        while True:
            if progress is not None and not progress(0, 100):
                raise EmbedCancelled()
            time.sleep(0.005)


# --- gallery-build: success writes a model_versions row ------------------------


def test_gallery_build_runs_to_done_and_writes_model_version_row(tmp_path):
    # An idle enqueue promotes immediately, the fake builder runs to completion, and
    # a successful (enough=True) build persists exactly one model_versions row (as a
    # draft) with the summary's fields, plus a status().result carrying its version_id.
    store = _store(tmp_path)
    builder = _SuccessGalleryBuilder(n_crops=4, n_cats=2, n_vectors=4)
    manager = TrainingManager(gallery_builder=builder)

    res = manager.enqueue_gallery_build(store, None)
    assert res["position"] == 0 and res["deduped"] is False

    assert _wait(lambda: not manager.running), "gallery-build job did not finish within timeout"
    st = manager.status()
    assert st["running"] is False
    assert st["error"] is None
    assert st["history"][0]["state"] == "done"
    assert st["history"][0]["kind"] == "gallery-build"

    assert st["result"]["enough"] is True
    vid = st["result"]["version_id"]
    versions = store.list_model_versions()
    assert len(versions) == 1
    assert versions[0]["id"] == vid
    assert versions[0]["status"] == "draft"
    assert versions[0]["kind"] == "gallery"
    assert versions[0]["n_cats"] == 2 and versions[0]["n_vectors"] == 4
    assert versions[0]["quality"] == "all"
    assert versions[0]["gallery_available"] is True  # the stub file the fake wrote is on disk


# --- gallery-build: enough=False writes no row, surfaces the message -----------


def test_gallery_build_not_enough_writes_no_row_and_surfaces_message(tmp_path):
    # A cold-start (enough=False) builder result is a clean 'done' (not 'failed')
    # that surfaces the friendly message via status().result, but persists no
    # model_versions row — distinct from a red 'failed' job.
    store = _store(tmp_path)

    def not_enough(store, out_dir, qualities=None, progress=None):
        return {
            "enough": False,
            "n_crops": 1,
            "n_cats": 1,
            "quality": "all",
            "message": "Not enough labelled data yet: 1 crops across 1 cat(s). "
                       "Grade representative crops as gallery, or widen the selection.",
        }

    manager = TrainingManager(gallery_builder=not_enough)
    manager.enqueue_gallery_build(store, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["history"][0]["state"] == "done"
    assert st["error"] is None
    assert st["result"]["enough"] is False
    assert "Not enough labelled data" in st["result"]["message"]
    assert store.list_model_versions() == []


# --- identify: success runs to done, resolving the active model ---------------


def test_identify_job_runs_to_done(tmp_path):
    # With an active model + gallery on disk, enqueue_identify resolves it, hands
    # the fake identifier the model id + gallery path + window bounds, and a
    # successful run is recorded 'done' with the identifier's n_identified surfaced.
    store = _store(tmp_path)
    model = _make_active_model(store)
    identifier = _SuccessIdentifier(n_identified=5)
    manager = TrainingManager(identifier=identifier)

    res = manager.enqueue_identify(store, since_id=10, until_id=20)
    assert res["position"] == 0 and res["deduped"] is False

    assert _wait(lambda: not manager.running), "identify job did not finish within timeout"
    st = manager.status()
    assert st["running"] is False
    assert st["error"] is None
    assert st["history"][0]["state"] == "done"
    assert st["history"][0]["kind"] == "identify"

    assert st["result"] == {
        "kind": "identify",
        "n_identified": 5,
        "since_id": 10,
        "until_id": 20,
    }
    assert identifier.calls == 1
    called_model_id, called_gallery_path, since_id, until_id = identifier.received[0]
    assert called_model_id == model["id"]
    assert called_gallery_path == model["gallery_path"]
    assert (since_id, until_id) == (10, 20)


# --- cancel: stop_event -> EmbedCancelled -> 'canceled', no row/side-effect ----


def test_cancel_yields_canceled(tmp_path):
    # Cancel sets stop_event; the fake polling progress sees it go falsy and raises
    # EmbedCancelled. The worker records 'canceled' (not 'failed' or 'done'), no
    # error is surfaced, and (since identify's writes are per-batch) nothing was
    # ever persisted for this run.
    store = _store(tmp_path)
    _make_active_model(store)
    cancel_identifier = _CancelIdentifier()
    manager = TrainingManager(identifier=cancel_identifier)
    manager.enqueue_identify(store, None, None)

    assert cancel_identifier.entered.wait(timeout=5), "cancelable job never started"
    manager.cancel()

    assert _wait(lambda: not manager.running), "job did not wind down after cancel"
    st = manager.status()
    assert st["history"][0]["state"] == "canceled"
    assert st["history"][0]["kind"] == "identify"
    assert st["history"][0]["error"] is None
    assert st["error"] is None


# --- fatal error surfaces as 'failed' ------------------------------------------


def test_raising_runner_yields_failed(tmp_path):
    # A non-cancel exception from the injected runner (here: identify, resolving a
    # real active model but then blowing up mid-run — e.g. a backbone that won't
    # load) is fatal to the job: recorded 'failed' with the message on both the
    # history record and status().error, and no model_versions row is disturbed.
    store = _store(tmp_path)
    _make_active_model(store)

    def boom(store, model, gallery_path, since_id, until_id, progress=None):
        raise RuntimeError("identifier boom")

    manager = TrainingManager(identifier=boom)
    manager.enqueue_identify(store, None, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["error"] == "identifier boom"
    assert st["history"][0]["state"] == "failed"
    assert st["history"][0]["error"] == "identifier boom"
    assert st["history"][0]["kind"] == "identify"
    # The pre-existing active model row is untouched by the failed run.
    assert len(store.list_model_versions()) == 1
    assert store.active_model() is not None


# --- dedup: only the running job, never pending; re-run after completion is fresh ---


def test_dedup_guards_running_double_click_but_not_pending_rerun(tmp_path):
    # While the head gallery-build job is provably running, an identical enqueue is
    # deduped onto it (position 0, deduped=True) — the double-click guard. A
    # DIFFERENT quality selection is not a duplicate and enqueues behind it. After
    # the queue drains, an identical request to a COMPLETED run is genuinely new
    # work (the labelled set may have grown) and promotes fresh, not deduped.
    store = _store(tmp_path)
    gated = _GatedGalleryBuilder()
    manager = TrainingManager(gallery_builder=gated)

    r1 = manager.enqueue_gallery_build(store, None)  # running
    assert r1["position"] == 0 and r1["deduped"] is False
    assert gated.entered.wait(timeout=5), "head job never started"

    # Identical to the RUNNING job -> deduped onto it at position 0, not enqueued.
    dup = manager.enqueue_gallery_build(store, None)
    assert dup["deduped"] is True and dup["position"] == 0
    assert manager.status()["queue"] == []  # nothing pending — the dup was dropped

    # A distinct quality selection is a real new job behind the running one.
    other = manager.enqueue_gallery_build(store, ["gallery"])
    assert other["deduped"] is False and other["position"] == 1
    assert [j["params"] for j in manager.status()["queue"]] == [["gallery"]]

    gated.release.set()
    assert _wait(lambda: not manager.running)
    # Both distinct jobs ran; the gated fake was invoked once per job.
    assert gated.calls == 2

    # A re-run identical to a COMPLETED (not pending) request is NOT a dedup.
    again = manager.enqueue_gallery_build(store, None)
    assert again["deduped"] is False and again["position"] == 0
    assert _wait(lambda: not manager.running)
    assert gated.calls == 3
    # Each run wrote its own model_versions row — three completed builds total.
    assert len(store.list_model_versions()) == 3

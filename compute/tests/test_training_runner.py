"""Tests for the Training-page job queue (compute/learning/runner.TrainingManager).

The manager is the direct sibling of ``AnalysisManager`` (see test_analysis.py's
manager section), so these mirror that style — a deterministic FAKE ``probe_runner``
stands in for the real embed→metrics→report pipeline, so the whole
queue/threading/lifecycle is exercised with NO torch, NO matplotlib, and no real
model. Two fakes cover the two shapes the manager cares about:

- ``_GatedProbe`` blocks inside the run (on a release ``Event``) so the head job can
  be held provably-running while a second enqueue's dedup / position is asserted —
  the analogue of ``GatedAnalyzer``.
- ``_CancelProbe`` polls its ``progress`` callback in a loop and raises
  ``EmbedCancelled`` the instant it goes falsy — exactly what the real
  ``embed_paths`` does when ``stop_event`` is set — so Cancel can be asserted to stop
  the run and write NO ``feasibility_runs`` row.

A real temp ``Store`` is used throughout (it needs no CV/torch), so the
success-path test can assert an actual ``feasibility_runs`` row is persisted and the
report file the fake writes is discoverable.
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


def _write_report(out_dir: str) -> None:
    """Write a stub ``feasibility.html`` where the real probe would, so the manager's
    ``feasibility_runs`` row resolves ``report_available=True`` afterwards."""
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "feasibility.html"), "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><title>stub</title>")


class _SuccessProbe:
    """A non-blocking fake that reports two progress ticks, writes a stub report, and
    returns an ``enough=True`` summary — the shape the manager persists into a row.

    Records how many times it was invoked so a re-run-after-completion test can prove a
    second, distinct run actually happened (not a dedup).
    """

    def __init__(self, n_crops: int = 4, n_cats: int = 2) -> None:
        self.calls = 0
        self.n_crops = n_crops
        self.n_cats = n_cats
        self.out_dirs: "list[str]" = []

    def __call__(self, store, out_dir, qualities=None, progress=None):
        self.calls += 1
        self.out_dirs.append(out_dir)
        if progress is not None:
            progress(0, self.n_crops)
            progress(self.n_crops, self.n_crops)
        _write_report(out_dir)
        return {
            "enough": True,
            "n_crops": self.n_crops,
            "n_cats": self.n_cats,
            "knn_accuracy": 0.9,
            "auc": 0.95,
            "threshold": 0.42,
            "quality": "all" if qualities is None else "+".join(qualities),
            "report_dir": out_dir,
        }


class _GatedProbe:
    """A fake that blocks inside the run until ``release`` is set — the head-job freezer.

    Sets ``entered`` once it is executing (so a test knows the job is provably running)
    then waits on ``release``, mirroring ``GatedAnalyzer``. On release it finishes as a
    normal successful run.
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
        _write_report(out_dir)
        return {
            "enough": True,
            "n_crops": 2,
            "n_cats": 2,
            "knn_accuracy": 0.5,
            "auc": 0.5,
            "threshold": 0.3,
            "quality": "all" if qualities is None else "+".join(qualities),
            "report_dir": out_dir,
        }


class _CancelProbe:
    """A fake that loops on ``progress`` and raises ``EmbedCancelled`` when it goes falsy.

    This is exactly the real ``embed_paths`` cancel contract: the manager's callback
    returns ``not stop_event.is_set()``, so once ``cancel()`` fires the next poll goes
    falsy and the run raises ``EmbedCancelled`` — the manager must record ``canceled``
    and write no row. ``entered`` lets the test hold the run provably-in-flight before
    cancelling, so the assertion is timing-free.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()

    def __call__(self, store, out_dir, qualities=None, progress=None):
        self.entered.set()
        while True:
            if progress is not None and not progress(0, 100):
                raise EmbedCancelled()
            time.sleep(0.005)


# --- lifecycle: enqueue promotes & runs, persists a row -----------------------


def test_enqueue_promotes_runs_and_writes_row(tmp_path):
    # An idle enqueue promotes immediately (position 0), the worker runs the fake to
    # completion, and a successful run persists exactly one feasibility_runs row plus
    # a status().result carrying its run_id.
    store = _store(tmp_path)
    probe = _SuccessProbe(n_crops=4, n_cats=2)
    manager = TrainingManager(probe_runner=probe)

    res = manager.enqueue_feasibility(store, None)
    assert res["position"] == 0 and res["deduped"] is False

    assert _wait(lambda: not manager.running), "training job did not finish within timeout"
    st = manager.status()
    assert st["running"] is False
    assert st["error"] is None
    assert st["history"][0]["state"] == "done"
    assert st["history"][0]["kind"] == "feasibility"

    # The success summary is stashed with a run_id, and the DB row matches.
    assert st["result"]["enough"] is True
    rid = st["result"]["run_id"]
    runs = store.feasibility_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == rid
    assert runs[0]["n_crops"] == 4 and runs[0]["n_cats"] == 2
    assert runs[0]["quality"] == "all"
    assert runs[0]["report_available"] is True  # the stub report the fake wrote is on disk


def test_progress_updates_done_and_total(tmp_path):
    # The probe's progress callback drives the ETA counters through _set_progress; after
    # the run the last-seen (done, total) is visible on status().
    store = _store(tmp_path)
    probe = _SuccessProbe(n_crops=7, n_cats=3)
    manager = TrainingManager(probe_runner=probe)
    manager.enqueue_feasibility(store, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["done"] == 7 and st["total"] == 7


# --- dedup: only the running job, never pending; re-run after completion is fresh ---


def test_dedup_guards_running_double_click_but_rerun_after_completion_is_fresh(tmp_path):
    # While the head job is provably running, an identical enqueue is deduped onto it
    # (position 0, deduped=True) — the double-click guard. A DIFFERENT quality is not a
    # duplicate and enqueues behind it. After the queue drains, an identical request is a
    # genuinely NEW run (the labelled set may have grown), so it promotes fresh.
    store = _store(tmp_path)
    gated = _GatedProbe()
    manager = TrainingManager(probe_runner=gated)

    r1 = manager.enqueue_feasibility(store, None)  # running
    assert r1["position"] == 0 and r1["deduped"] is False
    assert gated.entered.wait(timeout=5), "head job never started"

    # Identical to the RUNNING job → deduped onto it at position 0, not enqueued.
    dup = manager.enqueue_feasibility(store, None)
    assert dup["deduped"] is True and dup["position"] == 0
    assert manager.status()["queue"] == []  # nothing pending — the dup was dropped

    # A distinct quality selection is a real new job behind the running one.
    other = manager.enqueue_feasibility(store, ["gallery"])
    assert other["deduped"] is False and other["position"] == 1
    assert [j["params"] for j in manager.status()["queue"]] == [["gallery"]]

    gated.release.set()
    assert _wait(lambda: not manager.running)
    # Both distinct jobs ran; the gated fake was invoked once per job.
    assert gated.calls == 2

    # A re-run identical to a COMPLETED request is NOT a dedup — it promotes fresh.
    again = manager.enqueue_feasibility(store, None)
    assert again["deduped"] is False and again["position"] == 0
    assert _wait(lambda: not manager.running)
    assert gated.calls == 3


# --- cancel: stop_event -> EmbedCancelled -> 'canceled', no row ----------------


def test_cancel_stops_run_and_writes_no_row(tmp_path):
    # Cancel sets stop_event; the fake polling progress sees it go falsy and raises
    # EmbedCancelled. The worker records 'canceled' (not 'failed'), no feasibility_runs
    # row is written, and no error is surfaced.
    store = _store(tmp_path)
    cancel_probe = _CancelProbe()
    manager = TrainingManager(probe_runner=cancel_probe)
    manager.enqueue_feasibility(store, None)

    assert cancel_probe.entered.wait(timeout=5), "cancelable job never started"
    manager.cancel()

    assert _wait(lambda: not manager.running), "job did not wind down after cancel"
    st = manager.status()
    assert st["history"][0]["state"] == "canceled"
    assert st["history"][0]["error"] is None
    assert st["error"] is None
    assert store.feasibility_runs() == []  # a canceled run persists nothing


# --- fatal error surfaces as 'failed' -----------------------------------------


def test_probe_exception_records_failed(tmp_path):
    # A non-cancel exception from the probe (deps slipped past the endpoint pre-check, an
    # I/O error) is fatal to the job: recorded 'failed' with the message on both the
    # history record and status().error, and no row written.
    store = _store(tmp_path)

    def boom(store, out_dir, qualities=None, progress=None):
        raise RuntimeError("probe boom")

    manager = TrainingManager(probe_runner=boom)
    manager.enqueue_feasibility(store, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["error"] == "probe boom"
    assert st["history"][0]["state"] == "failed"
    assert st["history"][0]["error"] == "probe boom"
    assert store.feasibility_runs() == []


def test_not_enough_data_is_done_with_message_and_no_row(tmp_path):
    # A cold-start (enough=False) probe result is a clean 'done' that surfaces the friendly
    # message via status().result but writes no row — distinct from a red 'failed' job.
    store = _store(tmp_path)

    def not_enough(store, out_dir, qualities=None, progress=None):
        return {"enough": False, "n_crops": 1, "n_cats": 1, "quality": "all",
                "message": "Not enough labelled data yet: 1 crops across 1 cat(s)."}

    manager = TrainingManager(probe_runner=not_enough)
    manager.enqueue_feasibility(store, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["history"][0]["state"] == "done"
    assert st["error"] is None
    assert st["result"]["enough"] is False
    assert "Not enough labelled data" in st["result"]["message"]
    assert store.feasibility_runs() == []


# --- late cancel: a Cancel arriving AFTER the run completed is a no-op, not 'canceled' ---


def test_cancel_after_completion_is_done_and_keeps_row(tmp_path):
    # If the run RETURNS (embedded + persisted) and only THEN a cancel sets stop_event
    # before the worker's finally observes it, the run must record 'done' and keep its
    # row — a cancel that lost the race to completion is a harmless no-op, never a
    # 'canceled'-but-persisted contradiction. The fake sets the manager's stop_event
    # itself, right before returning success, to reproduce exactly that window.
    store = _store(tmp_path)

    class _LateCancelProbe:
        def __init__(self) -> None:
            self.manager = None

        def __call__(self, store, out_dir, qualities=None, progress=None):
            if progress is not None:
                progress(0, 2)
                progress(2, 2)
            _write_report(out_dir)
            self.manager.stop_event.set()  # cancel arrives after the work is done
            return {
                "enough": True, "n_crops": 2, "n_cats": 2, "knn_accuracy": 0.8,
                "auc": 0.9, "threshold": 0.4, "quality": "all", "report_dir": out_dir,
            }

    probe = _LateCancelProbe()
    manager = TrainingManager(probe_runner=probe)
    probe.manager = manager
    manager.enqueue_feasibility(store, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["history"][0]["state"] == "done"  # NOT 'canceled' — it completed
    assert st["result"]["enough"] is True
    assert len(store.feasibility_runs()) == 1  # the row it persisted survives the late cancel


# --- persist failure: the orphan report dir is cleaned up so prune can't miss it ---


def test_persist_failure_removes_orphan_report_dir(tmp_path, monkeypatch):
    # The probe writes its report dir, then add_feasibility_run raises (sqlite locked/full).
    # prune only sweeps dirs that HAVE a row, so the manager must delete the just-written
    # dir before the failure propagates — otherwise it accumulates as an unbounded orphan.
    store = _store(tmp_path)
    probe = _SuccessProbe()
    manager = TrainingManager(probe_runner=probe)

    def boom(*args, **kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(store, "add_feasibility_run", boom)
    manager.enqueue_feasibility(store, None)

    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["history"][0]["state"] == "failed"
    assert "db locked" in (st["error"] or "")
    assert probe.out_dirs and not os.path.isdir(probe.out_dirs[0])  # orphan dir removed
    assert store.feasibility_runs() == []

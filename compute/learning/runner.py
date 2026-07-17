"""The Training-page job queue: run the feasibility probe, a gallery-build, or an
identify pass in the background, one job at a time, cancelable. (Promotion is a
synchronous status flip on the store, deliberately NOT a queued job — see the
identification-gallery spec.)

This is the direct sibling of the oracle-sweep runner
(``compute/analysis/runner.AnalysisManager``): the two are structurally identical
walk-away queues — a daemon worker draining an in-memory FIFO one job at a time, a
single lock over *all* mutable state, a fresh ``stop_event`` per promotion, a
bounded finished-job history, and the load-bearing atomic "record terminal state →
clear running → promote next" transition in the worker's ``finally``. They are kept
as *separate instances* on purpose (see the training-page spec): training and
oracle sweeps are unrelated workflows and must not share a dedup namespace or
contend for one queue slot, even though they may run concurrently (each is serial
internally; simultaneous GPU pressure is accepted for a manual, infrequent action).

Where it DIVERGES from ``AnalysisManager``, all driven by the heterogeneous-job
decision:

- A ``_Job`` carries a ``kind`` (``'feasibility'`` | ``'gallery-build'`` |
  ``'identify'``) and a params payload, not a resolved ``Analyzer``. The worker
  dispatches on ``kind`` to the right run function, and the per-run timestamped
  output dir (feasibility report / gallery artifact) is assigned when the job
  *runs*, so it is NOT in the job or its dedup key.
- ``_enqueue`` dedups ONLY against the currently-running job (a double-click
  guard), NEVER against pending jobs. A sweep is identical work over immutable
  frames, so an identical pending sweep is a duplicate; a feasibility run instead
  reads the *current, growing* labelled set, so a re-run after more labelling is
  genuinely new work and must enqueue rather than be silently dropped onto a stale
  pending job.
- Progress is a generic ``done``/``total`` (no analyzer-specific ``present``). The
  worker hands the probe a ``progress(done, total)`` callback that both feeds the
  ETA counters (``_set_progress``) AND carries the cancel signal: it returns
  ``not stop_event.is_set()``, and the embed loop raises ``EmbedCancelled`` at the
  next batch boundary when it goes falsy — so Cancel actually interrupts the long
  embedding phase instead of no-op'ing until it is nearly done.
- On a successful feasibility run the worker WRITES the ``feasibility_runs`` row and
  prunes old report dirs; the probe orchestrator itself stays a pure compute+report
  function that never touches the DB (so the CLI can reuse it without persisting).

``probe_runner`` / ``gallery_builder`` / ``identifier`` are the three injection
seams (defaulting to ``run_feasibility_probe`` / ``build_gallery`` /
``run_identify``): a test passes fakes so the whole queue/threading/lifecycle is
exercisable with no torch/matplotlib and no real model. Importing this module stays
cheap — the probe's matplotlib is lazy-imported inside its chart helpers, and the
embedder's torch is lazy-imported inside its own methods (``gallery`` reaches torch
only through the ``Embedder``, so importing it here stays torch-free too).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from compute.identification.embed import EmbedCancelled
from compute.identification.gallery import build_gallery, run_identify
from compute.identification.probe import _quality_slug, run_feasibility_probe

if TYPE_CHECKING:
    from compute.collection.store import Store

logger = logging.getLogger(__name__)

# How many finished jobs ``status()`` reports back, most-recent-first. Bounded because
# the history is in-memory diagnostics for a returning operator, not a durable audit log
# — a restart drops it (a successful run's metrics persist in ``feasibility_runs``, and a
# re-enqueue re-runs cheaply). Mirrors ``AnalysisManager``'s ``_HISTORY_LIMIT``.
_HISTORY_LIMIT = 20

# Default cap on retained on-disk report dirs (overridable per-env). Rows in
# ``feasibility_runs`` are kept indefinitely (each is tiny); only the heavier report
# dirs are bounded — an aged-out run keeps its metrics row and reports
# ``report_available=False``.
_ENV_REPORTS_KEEP = "CAT_TRAINING_REPORTS_KEEP"
_DEFAULT_REPORTS_KEEP = "25"


@dataclass(frozen=True)
class _Job:
    """One queued (or running) training job, immutable once created.

    Carries everything the worker needs to dispatch and run the job and everything
    ``status()`` needs to describe it, but NOT the counters (those live on the manager
    and belong to whatever job is currently running) nor the store (a single instance
    shared by every job, held on the manager). ``kind`` selects the run function
    (``'feasibility'`` | ``'gallery-build'`` | ``'identify'``); ``params`` is the hashable
    job payload — for ``'feasibility'`` / ``'gallery-build'`` the ``qualities`` tuple
    (``None`` = all grades), for ``'identify'`` the ``(since_id, until_id)`` window bounds.
    The per-run timestamped output dir is assigned when the job *runs*, so it is
    deliberately NOT part of the job or its dedup key. ``label`` is a human-readable name
    for the logs only.
    """

    kind: str
    params: "tuple | None"
    label: str

    def dedup_key(self) -> tuple:
        """The job identity used to drop a double-click: ``(kind, params)``.

        Note the manager only ever compares this against the RUNNING job, never a pending
        one (see ``TrainingManager._enqueue``), so it guards a genuine double-click but
        lets a deliberate re-run after labelling enqueue.
        """
        return (self.kind, self.params)


class TrainingManager:
    """Owns the pending FIFO + the single active training job, draining one at a time.

    Mirrors ``AnalysisManager`` (and, through it, the collector's daemon-thread +
    stop-event shape): the head job runs on a background daemon thread; a
    ``threading.Event`` cancels it at the next progress boundary; a single
    ``threading.Lock`` guards *all* mutable state — the ``running`` flag, the counters,
    the per-job ``error``, the pending deque, the finished-job history, the last-result
    summary, and the ``stop_event`` reference itself — so the API's status poll, an
    external enqueue, a cancel, and the worker's own finished-job promotion never race.

    The load-bearing invariant is unchanged from ``AnalysisManager``: exactly one job runs
    at a time and the "record terminal state → clear ``running`` → promote the next"
    transition is ONE atomic lock hold in the worker's ``finally``, so an external
    ``enqueue`` can never observe ``running=False`` mid-promotion and double-start a
    worker. ``cancel`` sets ``stop_event`` under the same lock, so it can never race the
    promotion's ``stop_event`` swap.

    ``probe_runner`` / ``gallery_builder`` / ``identifier`` are the injection seams: they
    default to the real ``run_feasibility_probe`` / ``build_gallery`` / ``run_identify`` but
    a test passes fakes, so the queue/threading/lifecycle can be exercised with no torch,
    no matplotlib, and no real model.
    """

    def __init__(
        self,
        probe_runner=run_feasibility_probe,
        gallery_builder=build_gallery,
        identifier=run_identify,
    ) -> None:
        self._probe_runner = probe_runner
        self._gallery_builder = gallery_builder
        self._identifier = identifier
        # One lock guards every field below; taken briefly for reads (status) and writes
        # (enqueue / cancel / clear / _set_progress / _run's finally), NEVER held across the
        # heavy probe run itself.
        self._lock = threading.Lock()
        self._running = False
        # The running (or most-recently-run) job's kind/params, reported by status(). Held
        # separately so a status poll reads them under the lock without touching _current_job.
        self._kind: "str | None" = None
        self._params: "tuple | None" = None
        self._done = 0
        self._total = 0
        self._error: "str | None" = None
        # The last finished run's summary (a successful run's metrics + run_id, or a
        # not-enough-data message), so a poll that arrives after completion can render the
        # outcome and point the report iframe without a second fetch. None until a job
        # produces one; a failed/canceled job leaves the prior summary untouched.
        self._result: "dict | None" = None
        # Replaced with a fresh Event on every promotion so a prior job's set flag can't
        # pre-cancel the next one; the worker reads it (via the progress callback) between
        # batches, safe because only one job runs at a time.
        self.stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None
        # Pending jobs (FIFO: appended at the tail, promoted from the head) and the running
        # job descriptor. Invariant: pending is non-empty ONLY while a job is running.
        self._pending: "deque[_Job]" = deque()
        self._current_job: "_Job | None" = None
        # Finished-job outcomes, most-recent-first, bounded (appendleft + maxlen evicts the
        # oldest). Each record is written once (in the worker's finally) and never mutated.
        self._history: "deque[dict]" = deque(maxlen=_HISTORY_LIMIT)
        # The store every job runs against. All enqueues pass the same instance (one manager
        # is bound to one app's store), so re-assigning per enqueue is idempotent; held here
        # so the worker's finally can promote the next job without a store parameter.
        self._store: "Store | None" = None

    # --- Public enqueue API --------------------------------------------------------------

    def enqueue_feasibility(self, store: "Store", qualities: "list | None") -> dict:
        """Enqueue a feasibility validation run over the ``identified`` crops of ``qualities``.

        ``qualities`` is the crop-grade selection from the Validate panel's checkboxes —
        ``None`` (or empty) means "all grades", which is normalised to ``params=None`` so the
        dedup key and the report slug are stable regardless of how "all" was expressed. The
        heavy deps and the labelled-crop pre-check are the *endpoint's* concern (it runs
        ``Embedder.ensure_available()`` and ``count_identified_crops`` synchronously before
        calling here); this method just builds the job, records the store, and dedups+appends
        under the lock, promoting the head if idle (see ``_enqueue``).

        Returns ``{**status(), "position": int, "deduped": bool}`` — ``position`` is how many
        jobs must finish before this one starts (0 = running now), and ``deduped`` is True
        only when this exact request is already the *running* job (a double-click), never for
        a pending one.
        """
        params = tuple(qualities) if qualities else None
        label = "feasibility" if params is None else f"feasibility ({_quality_slug(params)})"
        job = _Job(kind="feasibility", params=params, label=label)
        return self._enqueue(store, job)

    def enqueue_gallery_build(self, store: "Store", qualities: "list | None") -> dict:
        """Enqueue a gallery build over the ``identified`` crops of ``qualities``.

        ``qualities`` is the crop-grade selection from the Build panel's checkboxes —
        ``None`` (or empty) means "all grades", normalised to ``params=None`` so the dedup
        key and the artifact-dir slug are stable regardless of how "all" was expressed. Like
        ``enqueue_feasibility`` the heavy deps + labelled-crop pre-check are the *endpoint's*
        concern; this just builds the job and dedups+appends under the lock (see
        ``_enqueue``). Same ``{**status(), "position", "deduped"}`` return.
        """
        params = tuple(qualities) if qualities else None
        label = "gallery-build" if params is None else f"gallery-build ({_quality_slug(params)})"
        job = _Job(kind="gallery-build", params=params, label=label)
        return self._enqueue(store, job)

    def enqueue_identify(self, store: "Store", since_id: "int | None", until_id: "int | None") -> dict:
        """Enqueue an identify pass over the active gallery for the ``[since_id, until_id]`` window.

        ``params`` is the ``(since_id, until_id)`` bounds tuple (either may be ``None`` = open
        end); the run resolves the active model itself. The endpoint guards "no active model"
        (409) and the zero-detection window before calling here. Same dedup-against-running-only
        semantics as the other enqueues — a re-run over the window is resumable work, deduped
        only against a genuine double-click. Same ``{**status(), "position", "deduped"}`` return.
        """
        params = (since_id, until_id)
        job = _Job(kind="identify", params=params, label="identify")
        return self._enqueue(store, job)

    def _enqueue(self, store: "Store", job: "_Job") -> dict:
        """Dedup (running only) + append ``job`` under the lock, promote the head if idle.

        The one place the pending deque grows. Under the lock: if this exact job (same
        ``dedup_key``) is the currently-RUNNING job, DROP it and return position 0 with
        ``deduped=True`` — a double-click guard. Crucially it does NOT dedup against pending
        jobs (unlike ``AnalysisManager``): a feasibility run reads the current, growing
        labelled set, so a re-run queued after more labelling is genuinely new work, not a
        duplicate. Otherwise append; if nothing is running, promote the head (position 0),
        else report the tail position (jobs ahead in line). The prepared thread is started
        AFTER releasing the lock so a concurrent status poll isn't blocked by thread spin-up.
        """
        thread: "threading.Thread | None" = None
        with self._lock:
            self._store = store
            key = job.dedup_key()
            # Dedup ONLY against the running job (double-click guard); never against pending.
            if self._running and self._current_job is not None and self._current_job.dedup_key() == key:
                return {**self._status_locked(), "position": 0, "deduped": True}
            self._pending.append(job)
            if self._running:
                # Appended at the tail behind the running job (and any earlier pending): its
                # index is len(pending) - 1, so jobs-ahead = (running) + index = len(pending).
                position = len(self._pending)
            else:
                thread = self._promote_locked()
                position = 0
            snapshot = {**self._status_locked(), "position": position, "deduped": False}
        if thread is not None:
            thread.start()
        return snapshot

    # --- Cancellation / queue controls (all lock-guarded) --------------------------------

    def cancel(self) -> None:
        """Cancel the running job; the worker stops at the next progress boundary and advances.

        Under the lock so it can never race the promotion's ``stop_event`` swap: it targets
        whatever job is ``running`` at the moment the lock is held. A no-op when idle (nothing
        to cancel — it does NOT arm a future job). The running job's next ``progress`` call
        returns falsy, the embed loop raises ``EmbedCancelled``, and the worker's ``finally``
        records the terminal state as ``canceled`` (writing NO ``feasibility_runs`` row) and
        promotes the next pending job. Does not block for the thread; poll ``status().running``.
        """
        with self._lock:
            if self._running:
                self.stop_event.set()

    def clear_pending(self) -> None:
        """Drop every pending job; leave the running job alone (it finishes normally).

        After this the running job completes and, finding an empty pending deque, promotes
        nothing — the manager goes idle.
        """
        with self._lock:
            self._pending.clear()

    def stop_all(self) -> None:
        """Stop everything: clear pending AND cancel the running job, atomically.

        Both under one lock hold so no pending job can be promoted between the clear and the
        cancel — the running job's ``finally`` then finds an empty deque and the manager goes
        idle. Paired with ``join`` at process exit to quiesce the worker before
        ``store.close()``.
        """
        with self._lock:
            self._pending.clear()
            if self._running:
                self.stop_event.set()

    def join(self, timeout: "float | None" = None) -> None:
        """Best-effort wait for the active worker thread to finish — for shutdown only.

        Pair with ``stop_all()`` at process exit: ``stop_all`` signals the worker to stop at
        the next progress boundary, then ``join`` waits for the run to actually return so the
        app can safely ``store.close()`` the shared connection without racing an in-flight
        ``add_feasibility_run`` / ``prune_feasibility_reports``. The thread reference is
        snapshotted under the lock but joined OUTSIDE it (never hold the lock across a join).
        A ``None``/already-finished thread returns at once; the worker is a daemon, so
        ``timeout`` bounds how long exit blocks.
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- Worker + promotion --------------------------------------------------------------

    def _promote_locked(self) -> "threading.Thread | None":
        """Prepare (but do NOT start) the next job's worker thread. Caller holds the lock.

        If a job is already running or the pending deque is empty, returns ``None``. Otherwise
        pops the head, resets the counters/kind/params/error for the new job, installs a FRESH
        ``stop_event`` (so a prior job's set flag can't pre-cancel it), flips ``running`` True,
        records the job as current, builds the daemon thread, and RETURNS it unstarted — the
        caller starts it after releasing the lock. Preparing (not starting) the thread here is
        what lets the worker's ``finally`` promote the next job inside its single atomic lock
        hold, so an external enqueue can never slip in and double-start.
        """
        if self._running or not self._pending:
            return None
        job = self._pending.popleft()
        self._current_job = job
        self._kind = job.kind
        self._params = job.params
        self._done = 0
        self._total = 0
        self._error = None
        self.stop_event = threading.Event()
        self._running = True
        thread = threading.Thread(
            target=self._run,
            args=(job, self._store),
            name="training",
            daemon=True,
        )
        self._thread = thread
        return thread

    def _run(self, job: "_Job", store: "Store") -> None:
        """Worker body: run one job, then atomically record its outcome + promote the next.

        Dispatches on ``job.kind`` to the run function. ``'feasibility'`` writes the durable
        ``feasibility_runs`` row on success, ``'gallery-build'`` inserts the ``model_versions``
        row after its artifact is on disk, and ``'identify'`` persists identifications per
        batch — persistence is the manager's concern, not the pure compute functions'. Three
        terminal paths converge in the ``finally``:

        - ``EmbedCancelled`` (an embed loop honored the stop signal) — the cancel path: no
          ``error``, ``stop_event`` is set, so the state is recorded ``canceled``. Feasibility
          and gallery-build wrote no persistent record (the embed precedes both the report/
          artifact write and the row insert); identify's per-batch writes are idempotent, so a
          cancel simply stops early with a partial, re-runnable result — nothing to undo.
        - any other ``Exception`` — fatal to THIS job (missing deps slipped past the endpoint
          pre-check, an I/O error writing the report); caught, logged, turned into ``failed``.
        - normal return — ``done``.

        The ``finally`` then, under a SINGLE lock hold: determines the terminal state, appends a
        history record, stashes the run's summary into ``_result`` (only when the run produced
        one — a failed/canceled job leaves the prior summary intact), clears ``running`` and the
        current-job slot, and prepares the next job's thread. Doing all of that atomically is the
        invariant that stops an external enqueue from double-starting a worker. The promoted
        thread (if any) is started only after the lock is released.
        """
        error: "str | None" = None
        result_summary: "dict | None" = None
        try:
            if job.kind == "feasibility":
                result_summary = self._run_feasibility(job, store)
            elif job.kind == "gallery-build":
                result_summary = self._run_gallery_build(job, store)
            elif job.kind == "identify":
                result_summary = self._run_identify(job, store)
            else:  # pragma: no cover - defensive: enqueue only ever builds the three kinds above
                raise ValueError(f"unknown training job kind: {job.kind!r}")
        except EmbedCancelled:
            # The probe's embed loop aborted at a batch boundary because the progress callback
            # went falsy — i.e. cancel(). stop_event is set, so the finally records 'canceled'
            # and writes no row; this is a clean stop, not a failure.
            logger.info("training job canceled during embedding: kind=%s", job.kind)
        except Exception as exc:
            logger.exception("training job failed: kind=%s", job.kind)
            error = str(exc)
        finally:
            # In ``finally`` (not after ``except``) so even a BaseException escaping the run —
            # SystemExit/KeyboardInterrupt/GeneratorExit, which ``except Exception`` deliberately
            # does not catch — still records the outcome and promotes the next job rather than
            # dying with ``running=True`` and wedging the whole queue.
            next_thread: "threading.Thread | None" = None
            with self._lock:
                if error is not None:
                    state = "failed"
                    # Surface the failure on status().error too, so a returning poll with an
                    # empty queue still shows it; a promoted successor resets it to None.
                    self._error = error
                elif result_summary is not None:
                    # The run RETURNED — it either persisted a row (enough) or short-circuited
                    # on cold-start (enough=False). Either way it completed, so a ``stop_event``
                    # set in the meantime is a cancel that lost the race to completion: a
                    # harmless no-op, NOT 'canceled'. Ordering result_summary ABOVE the
                    # stop_event check is what keeps the "canceled => wrote no row" invariant
                    # honest — a canceled run (EmbedCancelled) leaves result_summary None.
                    state = "done"
                elif self.stop_event.is_set():
                    state = "canceled"
                else:
                    state = "done"
                self._history.appendleft(
                    {
                        "kind": job.kind,
                        "params": list(job.params) if job.params is not None else None,
                        "state": state,
                        "error": error,
                    }
                )
                if result_summary is not None:
                    self._result = result_summary
                self._running = False
                self._current_job = None
                next_thread = self._promote_locked()
            if next_thread is not None:
                next_thread.start()

    def _run_feasibility(self, job: "_Job", store: "Store") -> "dict":
        """Run the feasibility probe for one job and, on success, persist its ``feasibility_runs`` row.

        Assigns the per-run timestamped report dir NOW (``<training_root>/<ts>-<slug>``, the
        slug tier-ordered so it is stable regardless of checkbox order), builds the
        ``progress`` callback that both drives the ETA counters and carries the cancel signal
        (returns ``not stop_event.is_set()`` — the embed loop raises ``EmbedCancelled`` when it
        goes falsy), and calls the injected probe runner. The probe never touches the DB, so on
        a successful (``enough``) run this writes the durable row and prunes old report dirs;
        on a not-enough-data run it writes NO row and returns the friendly message for the UI.
        Returns the summary dict stashed into ``status().result``.
        """
        ts = int(time.time() * 1000)
        slug = "all" if job.params is None else _quality_slug(job.params)
        out_dir = os.path.join(store.training_root, f"{ts}-{slug}")

        def progress(done: int, total: int) -> bool:
            self._set_progress(done, total)
            return not self.stop_event.is_set()

        result = self._probe_runner(
            store,
            out_dir,
            qualities=(list(job.params) if job.params else None),
            progress=progress,
        )

        if not result.get("enough"):
            # Cold-start / under-labelled: the probe embedded nothing and produced no report,
            # so there is nothing to persist — just surface the message as the run's outcome.
            return {
                "enough": False,
                "message": result.get("message"),
                "n_crops": result.get("n_crops"),
                "n_cats": result.get("n_cats"),
                "quality": result.get("quality"),
            }

        try:
            rid = store.add_feasibility_run(
                result["quality"],
                result["n_crops"],
                result["n_cats"],
                result["knn_accuracy"],
                result["auc"],
                result["threshold"],
                report_dir=os.path.basename(out_dir),
            )
        except Exception:
            # The report dir is already on disk but the row insert failed (locked/full/WAL).
            # prune_feasibility_reports only sweeps dirs that HAVE a row, so an orphan here
            # would never be bounded — remove it before the failure propagates.
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        # Bound the on-disk report footprint; the metrics rows are kept indefinitely.
        store.prune_feasibility_reports(int(os.environ.get(_ENV_REPORTS_KEEP, _DEFAULT_REPORTS_KEEP)))
        return {
            "enough": True,
            "run_id": rid,
            "quality": result["quality"],
            "n_crops": result["n_crops"],
            "n_cats": result["n_cats"],
            "knn_accuracy": result["knn_accuracy"],
            "auc": result["auc"],
            "threshold": result["threshold"],
            "report_dir": os.path.basename(out_dir),
        }

    def _run_gallery_build(self, job: "_Job", store: "Store") -> "dict":
        """Build one gallery and, on success, insert its ``model_versions`` row (as a draft).

        Assigns the per-version artifact dir NOW (``<models_root>/<ts>-<slug>``, ts-named so it
        is known BEFORE the row insert — the file-first ordering the store relies on: a crash
        orphans a harmless artifact dir, never a row without its ``.npz``), builds the same
        progress+cancel callback as ``_run_feasibility``, and calls the injected gallery builder
        (which writes ``gallery.npz`` but never touches the DB). A not-``enough`` result (cold
        start / decode failure) built no artifact, so it just ``rmtree``s the (possibly-absent)
        dir and returns the friendly summary with NO row. On success it inserts a ``status=draft``
        row and — mirroring ``_run_feasibility``'s orphan guard — ``rmtree``s the artifact if the
        insert fails (WAL/locked/full) before re-raising, so a failed insert never leaves a dir
        without its row. Returns the summary stashed into ``status().result``.
        """
        ts = int(time.time() * 1000)
        slug = "all" if job.params is None else _quality_slug(job.params)
        out_dir = os.path.join(store.models_root, f"{ts}-{slug}")

        def progress(done: int, total: int) -> bool:
            self._set_progress(done, total)
            return not self.stop_event.is_set()

        result = self._gallery_builder(
            store,
            out_dir,
            qualities=(list(job.params) if job.params else None),
            progress=progress,
        )

        if not result.get("enough"):
            # No artifact was written (the builder short-circuits before makedirs on both
            # insufficient-labels and decode-failure); rmtree is a defensive no-op here.
            shutil.rmtree(out_dir, ignore_errors=True)
            return {
                "kind": "gallery-build",
                "enough": False,
                "message": result.get("message"),
                "n_crops": result.get("n_crops"),
                "n_cats": result.get("n_cats"),
                "quality": result.get("quality"),
            }

        try:
            rid = store.add_model_version(
                status="draft",
                kind="gallery",
                backbone=result["backbone"],
                imgsz=result["imgsz"],
                n_cats=result["n_cats"],
                n_vectors=result["n_vectors"],
                threshold=result["threshold"],
                quality=result["quality"],
                metrics=result["metrics"],
                gallery_dir=os.path.basename(out_dir),
            )
        except Exception:
            # Artifact is on disk but the row insert failed — nothing references the dir, so
            # remove it before the failure propagates (mirrors _run_feasibility's report guard).
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        return {
            "kind": "gallery-build",
            "enough": True,
            "version_id": rid,
            "n_crops": result["n_crops"],
            "n_cats": result["n_cats"],
            "n_vectors": result["n_vectors"],
            "threshold": result["threshold"],
            "quality": result["quality"],
        }

    def _run_identify(self, job: "_Job", store: "Store") -> "dict":
        """Identify the ``[since_id, until_id]`` window against the ACTIVE gallery.

        Resolves the active model (its stored ``backbone``/``imgsz``/``gallery_path``); a race
        that leaves none active raises ``RuntimeError`` — the endpoint guards this too, but the
        run must not silently no-op. Builds the same progress+cancel callback and calls the
        injected identifier, which crops+embeds each detected frame, k=1-matches the gallery, and
        persists per batch through the store's idempotent, eviction-guarded writer. A cancel
        raises ``EmbedCancelled`` (handled by ``_run``'s finally) with no side-effect to undo —
        the per-batch writes are idempotent and a re-run resumes from the unidentified frames.
        Applies NO threshold; unknown is derived at read in ``events()``.
        """
        model = store.active_model()
        if model is None:
            raise RuntimeError("no active model")
        since_id, until_id = job.params

        def progress(done: int, total: int) -> bool:
            self._set_progress(done, total)
            return not self.stop_event.is_set()

        result = self._identifier(store, model, model["gallery_path"], since_id, until_id, progress)
        return {
            "kind": "identify",
            "n_identified": result["n_identified"],
            "since_id": since_id,
            "until_id": until_id,
        }

    # --- Progress hook (called by the probe via the run's callback) ----------------------

    def _set_progress(self, done: int, total: int) -> None:
        """Set the ETA counters (``done``/``total``) under the lock.

        The generic training-progress hook — replacing ``AnalysisManager.record(present)``,
        since a training job has no per-frame verdict. Called by the probe's ``embed_paths``
        once with ``(0, n)`` to set the denominator and after each batch with the cumulative
        crops embedded. Under the one lock so a concurrent ``status`` poll never reads a
        ratio torn between the two.
        """
        with self._lock:
            self._done = int(done)
            self._total = int(total)

    # --- Status ---------------------------------------------------------------------------

    def status(self) -> dict:
        """A consistent snapshot of the job state for the ``/api/training/status`` poll."""
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        """Build the status dict; caller holds the lock.

        Split out so the enqueue path can compose ``position``/``deduped`` onto a snapshot
        without re-acquiring the (non-reentrant) lock. ``params`` surfaces as a list (JSON) or
        ``None``; ``queue`` is FIFO order (next-to-run first) and ``history`` is
        most-recent-first, both bounded and holding never-mutated records. ``result`` is the
        most-recent finished run's summary (with ``run_id`` on success) or ``None``.
        """
        return {
            "running": self._running,
            "kind": self._kind,
            "params": list(self._params) if self._params is not None else None,
            "done": self._done,
            "total": self._total,
            "error": self._error,
            "queue": [
                {"kind": job.kind, "params": list(job.params) if job.params is not None else None}
                for job in self._pending
            ],
            "history": list(self._history),
            "result": self._result,
        }

    @property
    def running(self) -> bool:
        """Whether a training job is currently active (lock-guarded read)."""
        with self._lock:
            return self._running

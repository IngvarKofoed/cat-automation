"""The Cleanup-purge background job (admin-next P7).

A minimal single-job manager — the same daemon-worker + stop-event + one-lock shape
as ``AnalysisManager`` / ``TrainingManager``, but deliberately stripped down: there is
NO queue (a purge is a rare, operator-initiated, whole-store maintenance action, so a
second request while one runs is refused with ``busy`` rather than enqueued) and no
heavy deps (it drives pure ``Store`` methods, so importing this stays torch-free and a
test exercises the whole lifecycle with a real temp store and no GPU).

Two DATA-DESTRUCTIVE job kinds, both batched so the store lock is released between
batches (entries 102-105 — never hold it across a whole-store purge):

- ``nonmotion`` — drop ``motion = 0`` frames with ``id <= until_id`` through the
  eviction accounting path (``Store.purge_nonmotion_batch`` → ``_delete_frame_locked``),
  then record a ``purge_spans`` marker for the id range actually stripped so later
  scorecards/coverage over it warn "misses unmeasurable" (changelog 32's mechanism,
  folded in by ``motion_only_spans``). The upper bound is snapshotted by the endpoint
  BEFORE the run (``frame_id_bounds`` / a resolved date), so a whole-store purge can't
  chase the live collector's newer frames forever.
- ``orphan`` — sweep JPEGs under the FRAMES media dir that have no ``frames`` row (the
  changelog-42 leak), via ``Store.iter_media_relpaths`` + ``Store.delete_orphan_batch``.
  Scoped to ``_media_root`` only — the sibling dataset/avatar files are never walked.

The load-bearing invariant, as in the sibling managers, is the worker ``finally``: it
records the terminal state and clears ``running`` under ONE lock hold, so a status poll
never sees a torn state. Cancel is honored at batch boundaries; a cancel mid-run leaves
the store fully consistent (every batch commits under the lock with counters in lockstep)
and records only the span it actually purged.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from compute.collection.store import Store

logger = logging.getLogger(__name__)

# Batch sizes: how many frames/files one lock hold touches before releasing it so the
# collector and tuning sweeps aren't starved. Modest on purpose — a purge is background
# maintenance, not a latency-critical path.
_NONMOTION_BATCH = 500
_ORPHAN_BATCH = 200


class CleanupManager:
    """Owns the single active cleanup job (no queue); drives batched purges with cancel.

    One ``threading.Lock`` guards every mutable field (``running`` flag, progress
    counters, ``error``, ``result``, and the ``stop_event`` reference), so the API's
    status poll, a start, and a cancel never race the worker's own finished-job
    transition. A fresh ``stop_event`` is installed per job so a prior cancel can't
    pre-arm the next.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._kind: "str | None" = None
        self._done = 0
        self._total = 0
        self._error: "str | None" = None
        # Last finished run's summary, so a poll arriving after completion renders the
        # outcome. None until a job produces one; a failed job leaves the prior intact.
        self._result: "dict | None" = None
        self.stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

    # --- Public API ----------------------------------------------------------

    def start_nonmotion(
        self, store: "Store", until_id: "int | None", since_id: "int | None"
    ) -> dict:
        """Start the non-motion purge over ``id <= until_id`` (``None`` handled by the caller).

        ``since_id``/``until_id`` are the id-window the endpoint resolved+snapshotted
        (whole-store = ``frame_id_bounds``, older-than-date = ``resolve_ts_range``); they
        bound both the delete and the recorded ``purge_spans`` marker. Returns
        ``{**status, "started", "busy"}`` — ``started=False`` + ``busy=True`` if a job is
        already running (the endpoint maps that to 409).
        """
        return self._start(
            store, "nonmotion", lambda: self._run_nonmotion(store, until_id, since_id)
        )

    def start_orphan(self, store: "Store") -> dict:
        """Start the orphan-file sweep over the frames media dir. Same return as ``start_nonmotion``."""
        return self._start(store, "orphan", lambda: self._run_orphan(store))

    def cancel(self) -> None:
        """Signal the running job to stop at the next batch boundary; no-op when idle."""
        with self._lock:
            if self._running:
                self.stop_event.set()

    def stop_all(self) -> None:
        """Alias of ``cancel`` for shutdown parity with the other managers."""
        self.cancel()

    def join(self, timeout: "float | None" = None) -> None:
        """Best-effort wait for the worker to finish — for shutdown, before ``store.close()``.

        The purge writes through the store's shared connection, so exit must ``cancel``
        then ``join`` it before closing the DB, exactly like the analysis/training
        workers. Snapshot the thread under the lock but join OUTSIDE it.
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self) -> dict:
        """A consistent snapshot for the ``/api/cleanup/status`` poll."""
        with self._lock:
            return self._status_locked()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    # --- Internals -----------------------------------------------------------

    def _status_locked(self) -> dict:
        return {
            "running": self._running,
            "kind": self._kind,
            "done": self._done,
            "total": self._total,
            "error": self._error,
            "result": self._result,
        }

    def _start(self, store: "Store", kind: str, fn) -> dict:
        thread: "threading.Thread | None" = None
        with self._lock:
            if self._running:
                # One job at a time; refuse rather than enqueue (a purge is a rare
                # maintenance action, and two concurrent purges would race the media dir).
                return {**self._status_locked(), "started": False, "busy": True}
            self._kind = kind
            self._done = 0
            self._total = 0
            self._error = None
            self.stop_event = threading.Event()
            self._running = True
            thread = threading.Thread(target=self._worker, args=(kind, fn), name="cleanup", daemon=True)
            self._thread = thread
            snapshot = {**self._status_locked(), "started": True, "busy": False}
        thread.start()
        return snapshot

    def _worker(self, kind: str, fn) -> None:
        error: "str | None" = None
        result: "dict | None" = None
        try:
            result = fn()
        except Exception as exc:  # a purge is I/O + DB; surface a failure as terminal state
            logger.exception("cleanup job failed: kind=%s", kind)
            error = str(exc)
        finally:
            # ONE lock hold records the outcome and clears running, so a status poll
            # never observes a torn mid-transition state (the sibling managers' invariant).
            with self._lock:
                self._error = error
                if result is not None:
                    self._result = result
                self._running = False

    def _set_total(self, total: int) -> None:
        with self._lock:
            self._total = int(total)

    def _add_done(self, n: int) -> None:
        with self._lock:
            self._done += int(n)

    def _set_done(self, done: int) -> None:
        with self._lock:
            self._done = int(done)

    def _run_nonmotion(
        self, store: "Store", until_id: "int | None", since_id: "int | None"
    ) -> dict:
        """Drop non-motion frames in batches, then record the purge span actually stripped."""
        self._set_total(store.purge_nonmotion_estimate(until_id)["count"])
        deleted = 0
        last_max: "int | None" = None
        while not self.stop_event.is_set():
            n, max_id = store.purge_nonmotion_batch(until_id, _NONMOTION_BATCH)
            if n == 0:
                break  # window drained
            deleted += n
            last_max = max_id
            self._add_done(n)
        canceled = self.stop_event.is_set()
        # Record the span for the id range TRULY purged: the whole window on a clean
        # finish (up to until_id), or only up to the last frame we removed on a cancel —
        # so we never over-claim "unmeasurable" over frames whose non-motion samples
        # still exist. Nothing deleted → nothing to flag.
        span_recorded = False
        if deleted > 0 and since_id is not None:
            effective_end = last_max if canceled else (until_id if until_id is not None else last_max)
            if effective_end is not None:
                store.record_purge_span(since_id, effective_end)
                span_recorded = True
        return {
            "kind": "nonmotion",
            "deleted": deleted,
            "since_id": since_id,
            "until_id": until_id,
            "span_recorded": span_recorded,
            "canceled": canceled,
        }

    def _run_orphan(self, store: "Store") -> dict:
        """Sweep orphaned media files in batches, releasing the lock between each.

        Progress is INDETERMINATE by contract: ``done`` tracks files scanned so far,
        but ``total`` stays 0 — a sweep can't know its size without a full pre-walk,
        which the estimate would only double. A status consumer must treat
        ``total == 0`` (orphan kind) as indeterminate: show the scanned count or a
        spinner, never a ``done / total`` percentage (which would divide by zero).
        """
        deleted, freed, scanned = 0, 0, 0
        buf: "list[str]" = []
        for rel in store.iter_media_relpaths():
            if self.stop_event.is_set():
                break
            buf.append(rel)
            scanned += 1
            if len(buf) >= _ORPHAN_BATCH:
                res = store.delete_orphan_batch(buf)
                deleted += res["deleted"]
                freed += res["bytes"]
                buf = []
                self._set_done(scanned)
        if buf and not self.stop_event.is_set():
            res = store.delete_orphan_batch(buf)
            deleted += res["deleted"]
            freed += res["bytes"]
        self._set_done(scanned)
        return {
            "kind": "orphan",
            "deleted": deleted,
            "bytes": freed,
            "scanned": scanned,
            "canceled": self.stop_event.is_set(),
        }

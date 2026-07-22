"""The offline analysis sweep: drive one oracle over the whole store, in the background.

This is the compute analogue of the collector loop (``compute/collection/collector.py``)
turned around: where the collector pumps the *live* edge stream into the store always-on,
the sweep here reads frames back *out* of the store, runs a heavy oracle (YOLO / BSUV)
over each, and records a verdict — on demand, one job at a time, cancelable. It exists so
the edge's cheap MOG2 gate can be *validated* against a stronger reference without ever
putting that reference on the always-on ingest path (see the motion-gate-oracles spec).

Two pieces:

- ``run_analysis`` — the pure sweep: prepare the oracle, pick the store iterator that
  matches its statefulness, then for every frame decode → infer → write the verdict,
  surviving a bad frame and honoring a cancel between frames. It takes a ``manager`` only
  to report progress and read the stop flag, so it is exercisable with a stub manager.
- ``AnalysisManager`` — the walk-away job queue. Instead of refusing a second request
  while one runs, it now holds an in-memory FIFO of pending jobs and drains them one at a
  time (the GPU and the single SQLite connection are shared, so serial is the only correct
  execution — this is a status list, not a scheduler). It mirrors the collector's
  daemon-thread + stop-event style (see how ``create_app`` drives ``run_collector``): the
  head job runs on a background daemon thread, the rest wait, and each job's terminal
  outcome (done / failed / canceled) lands in a bounded history so a returning operator can
  tell a clean drain from a silent partial failure. The analyzer is resolved and its deps
  checked *synchronously* at enqueue so a bad name (``ValueError``) or a backend with
  missing optional deps (``ImportError``) surfaces to the HTTP caller up front instead of
  vanishing into the worker thread.

``cv2``/``numpy`` are imported lazily inside ``run_analysis`` (never at module top) — the
same discipline ``StreamFrame.image`` and the backends follow — so importing this module,
e.g. to hold an ``AnalysisManager`` from the API layer, never eagerly drags in the CV
stack even though a sweep obviously needs it.
"""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from compute.analysis import get_analyzer

if TYPE_CHECKING:
    from compute.analysis.base import Analyzer
    from compute.collection.store import Store

logger = logging.getLogger(__name__)

# Log a progress line every N written verdicts — enough to watch a long sweep advance in
# the logs (the UI polls ``status()`` for the live count), without flooding. It doubles as
# the throttle period for per-frame error logging, so a systematically bad store can't
# flood the log at frame rate. Mirrors the collector's ``_LOG_EVERY``.
_LOG_EVERY = 500

# How many finished jobs ``status()`` reports back, most-recent-first. Bounded because the
# history is in-memory diagnostics for a returning operator, not a durable audit log — a
# restart drops it (the verdicts it summarizes persist, and a re-enqueue resumes cheaply).
_HISTORY_LIMIT = 20

# Sentinel the stateless prefetch producer enqueues exactly once, from its ``finally``, to
# tell the consumer the stream is finished (or the producer has bailed). It is what lets the
# consumer's blocking ``get()`` always terminate instead of hanging on an empty queue.
_SENTINEL = object()


def _abort_put(q: "queue.Queue", item, abort: "threading.Event", timeout: float = 0.1) -> bool:
    """Put ``item`` on ``q``, but give up the instant ``abort`` is set. Returns whether it landed.

    A plain blocking ``put`` on a bounded queue deadlocks if the consumer has stopped
    draining (an operator cancel, or a consumer-side fatal such as ``write_analysis_batch``
    raising): the producer parks on the full queue forever and its ``join()`` never
    returns, wedging the whole one-at-a-time job queue. Looping a *timed* put and
    re-checking ``abort`` each turn lets the consumer's teardown unblock the producer by
    setting ``abort``. Returns True if the item was enqueued, False if it bailed because
    ``abort`` fired — this is the load-bearing anti-wedge primitive.
    """
    while not abort.is_set():
        try:
            q.put(item, timeout=timeout)
            return True
        except queue.Full:
            continue
    return False


def run_analysis(
    store: "Store",
    analyzer: "Analyzer",
    manager: "AnalysisManager",
    reanalyze: bool = False,
    since_id: "int | None" = None,
    until_id: "int | None" = None,
    motion_only: bool = False,
) -> None:
    """Run ``analyzer`` over every applicable frame in ``store``, writing a verdict each.

    Steps, in order:

    1. Resolve the scope: snapshot the frame horizon (``latest = store.latest_id()``),
       clamp the ceiling to ``until = min(until_id, latest)`` (or ``latest`` when
       ``until_id`` is ``None``), and carry ``since = since_id`` as the floor. ``since`` /
       ``until_id`` are the optional inclusive id bounds a group expands to (both ``None``
       = the whole store, exactly as before); the clamp keeps frames the collector inserts
       after now (id > ``until``) out of scope so ``done`` can't overrun ``total``.
    2. ``analyzer.prepare(store, since_id=since)`` — the one-time heavy setup (load
       weights; a windowed analyzer also primes its recent-frame window off the store
       here, warm-starting from the frames just *before* ``since`` when scoped).
    3. Pick the store iterator by ``analyzer.windowed`` and set the manager's ``total``,
       both scoped to ``[since, until]``:
       - stateless (YOLO) → ``store.iter_unanalyzed(name, since, until)`` over just the
         in-scope frames lacking a verdict, so a re-run resumes cheaply and skips done
         work; ``total`` is that scoped count.
       - windowed (BSUV/MOG2) → ``store.iter_time_order(since, until)`` over the *full*
         scoped set in time order, because its verdict depends on temporal neighbours and
         it must revisit every frame; ``total`` is the count of frames in ``[since,
         until]``.
    4. Drive the frames, by statefulness:
       - windowed (BSUV/MOG2) → the strict-time-order serial loop: read the JPEG off disk,
         ``cv2.imdecode`` to a BGR ndarray, ``analyzer.analyze`` it, ``store.write_analysis``
         the verdict. Order matters (rolling background), so no batching or prefetch.
       - stateless (YOLO) → a decode-ahead **producer** thread reads+decodes frames onto a
         bounded queue while THIS thread runs inference a batch at a time
         (``analyzer.batch_size`` per call, ``store.write_analysis_batch`` per flush), so
         decode overlaps the GPU instead of starving it. Batches never mix frame dimensions
         (a shape boundary flushes early), so each letterboxes exactly as the single-image
         path would — the batched path is pure throughput, not a verdict change.
       Either way, advance the manager's ``done`` (and ``present`` when the verdict is True).
    5. A read/decode/inference error on ONE frame is logged (throttled) and skipped — just
       like the collector's per-frame ``store.add`` failure handling — so a single corrupt
       or just-evicted frame can never abort a long sweep. On the stateless path a whole
       *batch* failing (most likely a CUDA OOM) falls back to per-image inference so one bad
       frame can't drop a batch of good verdicts, logged distinctly so a mis-sized batch is
       visible. A *fatal* error (``prepare`` failed, the iterator itself broke) propagates to
       the worker (``_run``), which records it into ``status().error`` — on the stateless
       path an iterator fault is captured in the producer and re-raised after its ``join``.

    ``manager.stop_event`` is checked between frames (windowed) or between batches
    (stateless), so a cancel takes effect promptly at the next boundary rather than
    mid-inference. The frame count that drives that
    verdict is stable for the job's lifetime: only one sweep runs at a time, so the store's
    concurrent inserts/evictions never reshuffle a stateless run's cursor (see the store
    iterators' keyset discipline).
    """
    # Lazy CV imports (see module docstring): kept out of module import so the API layer
    # can hold an AnalysisManager without touching the CV stack. Done once here, before
    # the loop, not per frame.
    import cv2
    import numpy as np

    def _read_decode(abs_path: str):
        """Read a stored JPEG off disk and decode it to a BGR ndarray, raising on failure.

        Shared by the windowed loop and the stateless producer so their decode / corrupt-
        frame handling can never diverge. ``imdecode`` returns ``None`` (not an exception)
        on a truncated/corrupt JPEG, so this raises ``ValueError`` to funnel that into the
        same log-and-skip path a read error takes.
        """
        with open(abs_path, "rb") as fh:
            buf = fh.read()
        image = cv2.imdecode(np.frombuffer(buf, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"failed to decode JPEG at {abs_path!r}")
        return image

    # Resolve the scope BEFORE prepare, so a windowed analyzer's warm-start (which reads
    # the store) primes for the same window the sweep will run over. ``until`` is clamped
    # to the live horizon so frames the collector inserts after now get id > ``until`` and
    # are out of scope (the next sweep picks them up), keeping ``done`` from overrunning
    # ``total`` while ingest runs; ``since`` is the (optional) range floor, passed through.
    latest = store.latest_id()
    until = latest if until_id is None else min(until_id, latest)
    since = since_id

    # prepare() takes the scope so a WINDOWED analyzer warm-starts from the frames
    # immediately BEFORE the window (see MogAnalyzer/BsuvAnalyzer._warm_start); a stateless
    # analyzer ignores since_id. Unscoped (since is None) this is exactly today's priming.
    analyzer.prepare(store, since_id=since)

    # Reanalyze clears the analyzer's prior verdicts only AFTER prepare() succeeds —
    # never before — so a prepare that fails (missing deps, no CUDA, model not wired)
    # leaves the old verdicts intact rather than wiping them for a sweep that never runs.
    # The clear is SCOPED to the same window the sweep re-verdicts (since/until): an
    # unscoped run clears the whole slot as before, but a scoped run clears ONLY the
    # window, so re-running an oracle over one group no longer discards every verdict
    # OUTSIDE it (the whole-store disagreement view and other windows keep their verdicts).
    if reanalyze:
        # motion_only scopes the clear to MOTION frames only (the tight Activity "Analyze"
        # path), so it doesn't wipe non-motion verdicts a breadth sweep produced. It applies
        # only to the stateless path: a WINDOWED oracle revisits + overwrites every frame
        # regardless, so its clear is never motion-scoped.
        store.clear_analysis(
            analyzer.name, since_id=since, until_id=until,
            motion_only=motion_only and not analyzer.windowed,
        )

    if analyzer.windowed:
        # Windowed oracle: drive the scoped window in time order so its rolling
        # recent-background window stays contiguous; the denominator is the count of
        # frames in [since, until].
        iterator = store.iter_time_order(since_id=since, until_id=until)
        total = store.count_in_range(since, until)
    else:
        # Stateless oracle: drive only the in-scope un-verdicted frames so a re-run is
        # cheap; the denominator is that outstanding TODO count within [since, until].
        iterator = store.iter_unanalyzed(analyzer.name, since_id=since, until_id=until, motion_only=motion_only)
        total = store.count_unanalyzed(analyzer.name, since_id=since, until_id=until, motion_only=motion_only)
    manager.set_total(total)

    logger.info(
        "analysis sweep started: analyzer=%s windowed=%s total=%d",
        analyzer.name,
        analyzer.windowed,
        total,
    )

    done = 0
    errors = 0

    if analyzer.windowed:
        # Windowed oracle: the strict-time-order per-frame loop, unchanged. Its rolling
        # background depends on seeing every frame in order, so it can be neither batched nor
        # prefetched-with-reordering — decode → analyze → write, one at a time, with a cancel
        # check and throttled log-and-skip between frames.
        for frame_id, abs_path in iterator:
            if manager.stop_event.is_set():
                logger.info("analysis sweep canceled: analyzer=%s after %d verdicts", analyzer.name, done)
                break
            try:
                image = _read_decode(abs_path)
                result = analyzer.analyze(image)
                store.write_analysis(frame_id, analyzer.name, result.verdict, result.score, result.detail)
            except Exception:
                # One bad frame — evicted between listing and read, corrupt bytes, a transient
                # inference error — must not abort the sweep. Log throttled (first, then every
                # _LOG_EVERY) so a systematic fault can't flood at frame rate, and move on.
                errors += 1
                if errors == 1 or errors % _LOG_EVERY == 0:
                    logger.exception("analysis: frame %s failed (%d skipped this run)", frame_id, errors)
                continue
            manager.record(bool(result.verdict))
            done += 1
            if done % _LOG_EVERY == 0:
                logger.info("analysis sweep: analyzer=%s %d/%d verdicts written", analyzer.name, done, total)
    else:
        # Stateless oracle: decode-ahead producer + batched GPU consumer (see the
        # yolo-sweep-throughput spec). One daemon thread reads+decodes frames onto a bounded
        # queue while THIS thread runs inference a batch at a time, so decode (which releases
        # the GIL in OpenCV) overlaps the GPU instead of starving it between per-frame reads.
        batch_size = getattr(analyzer, "batch_size", 1)
        q: "queue.Queue" = queue.Queue(maxsize=2 * batch_size)
        # Internal abort, DISTINCT from the user-facing stop_event: it couples the two threads
        # so no error path can wedge the job — the consumer's teardown sets it to unblock a
        # producer parked on a full queue before joining. stop_event is the operator cancel;
        # abort is the plumbing that guarantees the join always returns.
        abort = threading.Event()
        producer_fatal: dict = {}
        batch_errors = 0

        batch_ids: "list[int]" = []
        batch_images: list = []
        batch_shape = None

        def _producer() -> None:
            # Read+decode ahead of the GPU. A per-frame read/decode failure becomes a
            # skip-marker on the queue (counted+logged by the consumer, single-owner); an
            # iterator-level fault is fatal — captured and re-raised by the consumer after the
            # join, exactly as the old serial iterator error reached _run. The finally ALWAYS
            # enqueues the sentinel so the consumer's get() can never block forever.
            try:
                for frame_id, abs_path in iterator:
                    if abort.is_set() or manager.stop_event.is_set():
                        break
                    try:
                        image = _read_decode(abs_path)
                    except Exception:
                        # The producer can't own the skip counter (it must stay single-
                        # threaded), and the live exception context can't cross the queue, so
                        # it ships the formatted traceback for the consumer to log and count.
                        if not _abort_put(q, (frame_id, None, traceback.format_exc()), abort):
                            return
                        continue
                    if not _abort_put(q, (frame_id, image, None), abort):
                        return
            except Exception as exc:  # fatal: the iterator itself broke (sqlite, keyset bug)
                producer_fatal["exc"] = exc
            finally:
                _abort_put(q, _SENTINEL, abort)

        def _flush() -> None:
            # Persist one batch's verdicts. The path depends on whether the analyzer offers a
            # batched call: WITH analyze_batch we make ONE GPU call and fall back to per-image
            # only if it fails wholesale (most likely a CUDA OOM from too large a
            # CAT_YOLO_BATCH), logging that batch failure DISTINCTLY so a size silently
            # degrading to batch-1 throughput is visible. WITHOUT analyze_batch we go STRAIGHT
            # to the per-image loop — no doomed batch attempt to re-run (which would re-analyze
            # every already-done frame in the batch). The verdict write is fail-fast (see the
            # note at the write below). Empty batch → no-op.
            nonlocal done, errors, batch_errors
            if not batch_images:
                return

            batch_fn = getattr(analyzer, "analyze_batch", None)
            results = None
            if batch_fn is not None:
                try:
                    results = batch_fn(batch_images)
                except Exception:
                    batch_errors += 1
                    logger.exception(
                        "analysis: batch of %d failed (%d batch failures this run; likely CUDA "
                        "OOM) — retrying per-image",
                        len(batch_images),
                        batch_errors,
                    )
                    results = None

            rows: "list[tuple]" = []
            verdicts: "list[bool]" = []
            if results is not None:
                for fid, res in zip(batch_ids, results):
                    rows.append((fid, analyzer.name, res.verdict, res.score, res.detail))
                    verdicts.append(bool(res.verdict))
            else:
                # No batched call (backend lacks analyze_batch), or the batch failed: run each
                # frame alone so one bad frame can't drop the rest — and, for the no-batch
                # backend, so frames before a failure aren't analyzed a second time.
                for fid, img in zip(batch_ids, batch_images):
                    try:
                        res = analyzer.analyze(img)
                    except Exception:
                        errors += 1
                        if errors == 1 or errors % _LOG_EVERY == 0:
                            logger.exception("analysis: frame %s failed (%d skipped this run)", fid, errors)
                        continue
                    rows.append((fid, analyzer.name, res.verdict, res.score, res.detail))
                    verdicts.append(bool(res.verdict))

            # A verdict-write error is intentionally NOT caught here: the store is one
            # connection under one lock (+ WAL + busy_timeout), so the classic transient
            # SQLITE_BUSY can't arise from the concurrent collector; a real write failure
            # (disk full / I/O error) is persistent, and failing the job fast — data-safe,
            # since the batch's frames stay un-verdicted and resume via iter_unanalyzed —
            # surfaces it far better than silently skipping every batch to a green sweep.
            store.write_analysis_batch(rows)
            for v in verdicts:
                manager.record(v)
                done += 1
                if done % _LOG_EVERY == 0:
                    logger.info("analysis sweep: analyzer=%s %d/%d verdicts written", analyzer.name, done, total)
            batch_ids.clear()
            batch_images.clear()

        producer = threading.Thread(target=_producer, name="analysis-producer", daemon=True)
        producer.start()
        try:
            while True:
                item = q.get()
                if item is _SENTINEL:
                    _flush()
                    break
                frame_id, image, exc_text = item
                if image is None:
                    # Skip-marker: the producer hit a read/decode failure. The consumer is the
                    # single owner of the skip counter + throttled logging, so the per-frame
                    # log-and-skip cadence stays exact across the thread hand-off (logger.error
                    # with the carried text — the producer's exception context can't cross).
                    errors += 1
                    if errors == 1 or errors % _LOG_EVERY == 0:
                        logger.error(
                            "analysis: frame %s failed (%d skipped this run)\n%s", frame_id, errors, exc_text
                        )
                    continue
                # Shape-boundary chunk: never mix frame dimensions in one predict() call, so
                # every batch letterboxes exactly as the single-image path would (kills the
                # letterbox verdict-drift risk from an operator clip-rect change mid-collection).
                shape = image.shape
                if batch_images and shape != batch_shape:
                    _flush()
                if not batch_images:
                    batch_shape = shape
                batch_ids.append(frame_id)
                batch_images.append(image)
                if len(batch_images) >= batch_size:
                    _flush()
                    # Cancel is checked between batches: on stop, arm abort so the producer
                    # unblocks and stop draining new frames into batches.
                    if manager.stop_event.is_set():
                        logger.info("analysis sweep canceled: analyzer=%s after %d verdicts", analyzer.name, done)
                        abort.set()
                        break
        finally:
            # Runs on EVERY consumer exit — normal sentinel, cancel, or a consumer-side fatal
            # like write_analysis_batch raising. Set abort and DRAIN so a producer parked on a
            # full queue unblocks, then join it: no path may leave a running producer or block
            # forever (the load-bearing anti-wedge invariant).
            abort.set()
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            producer.join()

        # After the join: if the producer captured a fatal iterator error, re-raise it here so
        # _run records it into status().error exactly as the old serial loop did. (A consumer-
        # side fatal already propagated out of the try/finally above and never reaches this
        # line, so it surfaces as the job's error too.)
        if producer_fatal.get("exc") is not None:
            raise producer_fatal["exc"]

    logger.info("analysis sweep finished: analyzer=%s %d verdicts, %d skipped", analyzer.name, done, errors)


@dataclass(frozen=True)
class _Job:
    """One queued (or running) sweep, immutable once created.

    Carries everything the worker needs to run the sweep and everything ``status()`` needs
    to describe it, but NOT the counters (those live on the manager and belong to whatever
    job is currently running) nor the store (a single instance shared by every job, held on
    the manager). ``kind`` is the oracle/slot id reported to the UI
    (``'yolo'``/``'bsuv'``/``'mog2:baseline'``/``'mog2:candidate'``); ``name`` is set only
    for a registry oracle (unused after resolution, kept for provenance); ``analyzer`` is
    the resolved backend instance held from enqueue so the worker doesn't re-resolve.
    ``params`` is the hashable MOG2 param tuple for a pre-built slot (``None`` for the fixed
    oracles) — it is IN the dedup key so re-running ``mog2:candidate`` with *different*
    params over the same window is a distinct job (the tune loop), not a duplicate.
    """

    kind: str
    analyzer: "Analyzer"
    name: "str | None"
    reanalyze: bool
    since_id: "int | None"
    until_id: "int | None"
    params: "tuple | None"
    label: str
    motion_only: bool = False

    def dedup_key(self) -> tuple:
        """The full job identity used to drop a duplicate enqueue.

        ``(kind, params, reanalyze, since_id, until_id, motion_only)`` — the dedup key.
        ``params`` distinguishes two MOG2 candidates over the same window (different params
        → different jobs); ``reanalyze`` distinguishes a plain sweep from a re-verdict of the
        same oracle+window (otherwise a re-run would dedup away against the earlier run and
        silently never happen); ``motion_only`` keeps a tight motion-scoped sweep distinct
        from a full sweep of the same oracle+window, so one never dedups away the other.
        """
        return (self.kind, self.params, self.reanalyze, self.since_id, self.until_id, self.motion_only)


class AnalysisManager:
    """Owns the pending FIFO + the single active job, and drains the queue one at a time.

    Mirrors the collector's daemon-thread + stop-event shape (see
    ``compute/collection/collector.py`` and how ``create_app`` drives it): the head job runs
    on a background daemon thread; a ``threading.Event`` cancels it between frames; a single
    ``threading.Lock`` guards *all* mutable state — the ``running`` flag, the counters, the
    per-job ``error``, the pending deque, the finished-job history, and the ``stop_event``
    reference itself — so the API's status poll, an external enqueue, a cancel, and the
    worker's own finished-job promotion never race.

    The load-bearing invariant is that exactly one job runs at a time and the
    "record terminal state → clear ``running`` → promote the next" transition is ONE atomic
    lock hold in the worker's ``finally``: an external ``enqueue`` can therefore never
    observe ``running=False`` mid-promotion and double-start a second worker. ``cancel``
    sets ``stop_event`` *under the same lock*, so it can never race the promotion's
    ``stop_event`` swap — it targets whatever is running at the moment the lock is held (a
    cancel that loses the race to a natural completion is a harmless no-op, not a mis-fire
    against the successor). There is no more "refuse a second job": a second request
    enqueues and waits.

    ``resolver`` is the injection seam: it defaults to the package registry
    ``get_analyzer`` but a test can pass a stub returning a fake analyzer, so the manager's
    queue/threading/lifecycle can be exercised with no real model and none of its heavy deps.
    """

    def __init__(self, resolver=get_analyzer) -> None:
        self._resolver = resolver
        # One lock guards every field below; taken briefly for reads (status) and writes
        # (enqueue / cancel / clear / set_total / record / _run's finally), NEVER held
        # across the inference.
        self._lock = threading.Lock()
        self._running = False
        self._analyzer: "str | None" = None
        self._done = 0
        self._total = 0
        self._present = 0
        self._error: "str | None" = None
        # The active job's optional id-range scope (a group's bounds, or None on either
        # side for unbounded) — reported by status() so the UI can show "running MOG2
        # over <group>" rather than silently sweeping the whole store.
        self._since_id: "int | None" = None
        self._until_id: "int | None" = None
        # Replaced with a fresh Event on every promotion so a prior job's set flag can't
        # pre-cancel the next one; the worker reads it directly between frames, which is
        # safe because only one job runs at a time — the reference is only ever swapped by a
        # promotion, which happens after this job's run_analysis has returned.
        self.stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None
        # Pending jobs (FIFO: appended at the tail, promoted from the head) and the running
        # job descriptor. Invariant: pending is non-empty ONLY while a job is running (an
        # idle enqueue promotes its job immediately, and a finished job promotes the next),
        # so ``not running`` implies an empty pending deque.
        self._pending: "deque[_Job]" = deque()
        self._current_job: "_Job | None" = None
        # Finished-job outcomes, most-recent-first, bounded — appendleft + maxlen evicts the
        # oldest automatically. Each record is written once (in the worker's finally) and
        # never mutated, so status() can hand out the list without a per-entry copy.
        self._history: "deque[dict]" = deque(maxlen=_HISTORY_LIMIT)
        # The store every job sweeps. All enqueues pass the same instance (one manager is
        # bound to one app's store), so re-assigning it per enqueue is idempotent; held here
        # so the worker's finally can promote the next job without a store parameter.
        self._store: "Store | None" = None

    # --- Public enqueue API (replaces the old refuse-second-job start/start_analyzer) ----

    def enqueue_named(
        self,
        store: "Store",
        name: str,
        reanalyze: bool = False,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
        motion_only: bool = False,
    ) -> dict:
        """Resolve a registered oracle by ``name`` and enqueue a sweep of it.

        The name-based entry point for the fixed oracles (yolo/bsuv): it resolves the
        backend through ``self._resolver`` (a bad name raises ``ValueError`` → 400) and
        runs ``ensure_available()`` SYNCHRONOUSLY (an ``ImportError`` from a backend whose
        optional deps are absent propagates → 503 with the install hint) — both *before*
        the job is enqueued and while the HTTP handler is still on the stack, so a
        bad-name / missing-deps request fails at request time instead of vanishing into the
        worker as a delayed ``status().error``. Then, under the lock, the job is deduped and
        appended, and the head is promoted if idle (see ``_enqueue``).

        ``reanalyze`` and the optional ``since_id`` / ``until_id`` scope bounds ride through
        to the worker unchanged (see ``run_analysis``). Returns ``{**status(), "position":
        int, "deduped": bool}`` — ``position`` is how many jobs must finish before this one
        starts (0 = running now), and ``deduped`` is True when an identical job was already
        pending/running so this request was dropped onto it.
        """
        analyzer = self._resolver(name)
        analyzer.ensure_available()
        job = _Job(
            kind=name,
            analyzer=analyzer,
            name=name,
            reanalyze=bool(reanalyze),
            since_id=since_id,
            until_id=until_id,
            params=None,
            label=name,
            motion_only=bool(motion_only),
        )
        return self._enqueue(store, job)

    def enqueue_analyzer(
        self,
        store: "Store",
        analyzer: "Analyzer",
        reanalyze: bool = False,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ) -> dict:
        """Enqueue a sweep for an ALREADY-CONSTRUCTED analyzer instance (the MOG2 tuning path).

        The instance-based seam the tuning path uses directly: a ``MogAnalyzer(params, slot)``
        needs params from the run request, which the name→instance registry can't supply, so
        it is constructed by the caller and handed here without any ``ANALYZER_NAMES`` entry.
        ``ensure_available()`` runs SYNCHRONOUSLY here (missing/broken OpenCV → ``ImportError``
        → 503) before the job is enqueued. ``kind`` is the instance's own ``name`` (e.g.
        ``'mog2:candidate'``); its MOG2 params are captured into the dedup key as a hashable
        tuple, so two candidates with *different* params over the same window are distinct
        jobs (the tune loop) while a same-params double-click is dropped.

        ``reanalyze`` and the optional scope bounds ride along to the worker, where the
        verdict clear happens only after a successful ``prepare`` (see ``run_analysis``), so
        a deps-missing run can't wipe verdicts. Returns the same shape as ``enqueue_named``.
        """
        analyzer.ensure_available()
        raw_params = getattr(analyzer, "_params", None)
        params = tuple(raw_params) if raw_params is not None else None
        job = _Job(
            kind=analyzer.name,
            analyzer=analyzer,
            name=None,
            reanalyze=bool(reanalyze),
            since_id=since_id,
            until_id=until_id,
            params=params,
            label=analyzer.name,
        )
        return self._enqueue(store, job)

    def _enqueue(self, store: "Store", job: "_Job") -> dict:
        """Dedup + append ``job`` under the lock, promote the head if idle, start outside.

        The one place the pending deque grows. Under the lock: if an identical job (same
        ``dedup_key``) is already the running job or already pending, DROP this one and
        return the existing job's position with ``deduped=True`` (guards a double-click).
        Otherwise append; if nothing is running, promote the head — which pops THIS job
        (pending was empty by the invariant) and prepares its thread — and report position
        0; if a job is running, report the tail position (jobs ahead of it in line). The
        prepared thread is started AFTER releasing the lock so a concurrent status poll isn't
        blocked by thread spin-up, mirroring the collector's start-outside-the-lock pattern.
        """
        thread: "threading.Thread | None" = None
        with self._lock:
            self._store = store
            key = job.dedup_key()
            # Dedup against the running job.
            if self._running and self._current_job is not None and self._current_job.dedup_key() == key:
                return {**self._status_locked(), "position": 0, "deduped": True}
            # Dedup against a pending job (position = jobs ahead: 1 running + its index).
            for index, pending_job in enumerate(self._pending):
                if pending_job.dedup_key() == key:
                    return {**self._status_locked(), "position": index + 1, "deduped": True}
            # Not a duplicate: append and, if idle, promote it to running immediately.
            self._pending.append(job)
            if self._running:
                # Appended at the tail behind the running job (and any earlier pending):
                # its index is len(pending) - 1, so jobs-ahead = (running) + index = len.
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
        """Cancel the running job; the worker stops at the next frame boundary and advances.

        Under the lock so it can never race the promotion's ``stop_event`` swap: it targets
        whatever job is ``running`` at the moment the lock is held. A no-op when idle (there
        is nothing to cancel — unlike the old bare ``stop_event.set()``, this does NOT arm a
        future job). The worker's ``finally`` records the terminal state as ``canceled`` and
        promotes the next pending job. Does not block for the thread to finish; the caller
        polls ``status().running`` to watch it wind down.
        """
        with self._lock:
            if self._running:
                self.stop_event.set()

    def clear_pending(self) -> None:
        """Drop every pending job; leave the running job alone (it finishes normally).

        The "clear the queue" control: after this the running job completes and, finding an
        empty pending deque, promotes nothing — the manager goes idle.
        """
        with self._lock:
            self._pending.clear()

    def stop_all(self) -> None:
        """Stop everything: clear pending AND cancel the running job, atomically.

        Both under one lock hold so no pending job can be promoted between the clear and the
        cancel — the running job's ``finally`` then finds an empty deque and the manager goes
        idle. The unsurprising "stop the whole queue" button.
        """
        with self._lock:
            self._pending.clear()
            if self._running:
                self.stop_event.set()

    def join(self, timeout: "float | None" = None) -> None:
        """Best-effort wait for the active worker thread to finish — for shutdown only.

        Pair with ``stop_all()`` at process exit: ``stop_all`` signals the worker to
        stop at the next batch boundary, then ``join`` waits for ``run_analysis`` to
        actually return so the app can safely ``store.close()`` the shared connection
        without racing an in-flight ``write_analysis_batch`` / iterator fetch. The
        thread reference is snapshotted under the lock but joined OUTSIDE it (never
        hold the lock across a join). A ``None``/already-finished thread returns at
        once; the worker is a daemon, so the ``timeout`` bounds how long exit blocks.
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- Worker + promotion --------------------------------------------------------------

    def _promote_locked(self) -> "threading.Thread | None":
        """Prepare (but do NOT start) the next job's worker thread. Caller holds the lock.

        If a job is already running or the pending deque is empty, returns ``None`` (nothing
        to promote). Otherwise pops the head, resets the counters/scope/error for the new
        job, installs a FRESH ``stop_event`` (so a prior job's set flag can't pre-cancel it),
        flips ``running`` True, records the job as current, builds the daemon thread, and
        RETURNS it unstarted — the caller starts it after releasing the lock. Preparing the
        thread here (rather than starting it) is what lets the worker's ``finally`` promote
        the next job inside its single atomic lock hold: recording the terminal state,
        clearing ``running``, and readying the successor are indivisible, so an external
        enqueue can never slip in and double-start.
        """
        if self._running or not self._pending:
            return None
        job = self._pending.popleft()
        self._current_job = job
        self._analyzer = job.kind
        self._done = 0
        self._total = 0
        self._present = 0
        self._error = None
        self._since_id = job.since_id
        self._until_id = job.until_id
        self.stop_event = threading.Event()
        self._running = True
        thread = threading.Thread(
            target=self._run,
            args=(job, self._store),
            name="analysis",
            daemon=True,
        )
        self._thread = thread
        return thread

    def _run(self, job: "_Job", store: "Store") -> None:
        """Worker body: run one sweep, then atomically record its outcome + promote the next.

        Per-frame failures are handled inside ``run_analysis`` (log-and-skip); anything that
        reaches here is fatal to THIS job (``prepare`` failed, the store iterator broke) — it
        is caught, logged, and turned into the job's terminal ``error``. The ``finally`` then,
        **under a single lock hold**: determines the terminal state (exception → ``failed``;
        else the current ``stop_event`` set → ``canceled``; else ``done``), appends a history
        record carrying that state plus this job's final ``done``/``present`` counts, clears
        ``running`` and the current-job slot, and prepares the next job's thread. Doing all of
        that atomically is the invariant that stops an external enqueue from observing
        ``running=False`` mid-promotion and starting a second worker. The promoted thread (if
        any) is started only after the lock is released.
        """
        error: "str | None" = None
        try:
            run_analysis(store, job.analyzer, self, job.reanalyze, job.since_id, job.until_id, job.motion_only)
        except Exception as exc:
            logger.exception("analysis sweep failed")
            error = str(exc)
        finally:
            # Promotion lives in ``finally`` (not after the ``except``) so that even a
            # BaseException escaping the sweep — SystemExit/KeyboardInterrupt/GeneratorExit,
            # which ``except Exception`` deliberately does not catch — still records the
            # outcome and promotes the next job rather than dying with ``running=True`` and
            # wedging the whole queue. A BaseException still propagates after this runs; it
            # just no longer leaves the manager permanently busy.
            next_thread: "threading.Thread | None" = None
            with self._lock:
                if error is not None:
                    state = "failed"
                    # Surface the failure on status().error too, so a returning poll with an
                    # empty queue still shows it; a promoted successor resets it to None.
                    self._error = error
                elif self.stop_event.is_set():
                    state = "canceled"
                else:
                    state = "done"
                self._history.appendleft(
                    {
                        "kind": job.kind,
                        "since_id": job.since_id,
                        "until_id": job.until_id,
                        "state": state,
                        "error": error,
                        "done": self._done,
                        "present": self._present,
                    }
                )
                self._running = False
                self._current_job = None
                next_thread = self._promote_locked()
            if next_thread is not None:
                next_thread.start()

    # --- Progress hooks (called by run_analysis) -----------------------------------------

    def set_total(self, total: int) -> None:
        """Set the progress denominator once the sweep has counted its frames.

        Called by ``run_analysis`` after it has chosen its iterator. Under the lock so a
        concurrent ``status`` poll always reads a consistent snapshot.
        """
        with self._lock:
            self._total = int(total)

    def record(self, present: bool) -> None:
        """Count one written verdict: ``done`` += 1 always, ``present`` += 1 when True.

        The sweep's per-frame progress hook. ``present`` is the oracle's boolean verdict (a
        cat / foreground was seen), so ``present``/``done`` is the live hit rate the UI can
        show. Both increments happen under the one lock so a poll never reads a ratio torn
        between them.
        """
        with self._lock:
            self._done += 1
            if present:
                self._present += 1

    # --- Status ---------------------------------------------------------------------------

    def status(self) -> dict:
        """A consistent snapshot of the job state for the ``/api/analysis/status`` poll.

        The running job's counters/scope PLUS the pending ``queue`` (FIFO order,
        next-to-run first) and the finished-job ``history`` (most-recent-first, bounded).
        ``since_id`` / ``until_id`` report the active job's id-range scope (both ``None``
        when the sweep runs over the whole store), so the UI can show which window a run or
        re-run is covering rather than assuming whole-store.
        """
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        """Build the status dict; caller holds the lock.

        Split out so the enqueue path can compose ``position``/``deduped`` onto a snapshot
        without re-acquiring the (non-reentrant) lock. The ``history`` list is a shallow copy
        of never-mutated records, so the caller can't perturb the deque.
        """
        return {
            "running": self._running,
            "analyzer": self._analyzer,
            "done": self._done,
            "total": self._total,
            "present": self._present,
            "error": self._error,
            "since_id": self._since_id,
            "until_id": self._until_id,
            "queue": [
                {
                    "kind": job.kind,
                    "since_id": job.since_id,
                    "until_id": job.until_id,
                    "reanalyze": job.reanalyze,
                }
                for job in self._pending
            ],
            "history": list(self._history),
        }

    @property
    def running(self) -> bool:
        """Whether a sweep is currently active (lock-guarded read)."""
        with self._lock:
            return self._running

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
- ``AnalysisManager`` — owns the single active job's ``(thread, stop_event, counters,
  error)`` and serializes it, mirroring the collector's daemon-thread + stop-event style
  (see how ``create_app`` drives ``run_collector``). A second ``start`` while one runs is
  refused; the analyzer is resolved *synchronously* in ``start`` so a bad name
  (``ValueError``) or a backend with missing optional deps (``ImportError``) surfaces to
  the HTTP caller instead of vanishing into the worker thread.

``cv2``/``numpy`` are imported lazily inside ``run_analysis`` (never at module top) — the
same discipline ``StreamFrame.image`` and the backends follow — so importing this module,
e.g. to hold an ``AnalysisManager`` from the API layer, never eagerly drags in the CV
stack even though a sweep obviously needs it.
"""
from __future__ import annotations

import logging
import threading
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


def run_analysis(
    store: "Store", analyzer: "Analyzer", manager: "AnalysisManager", reanalyze: bool = False
) -> None:
    """Run ``analyzer`` over every applicable frame in ``store``, writing a verdict each.

    Steps, in order:

    1. ``analyzer.prepare(store)`` — the one-time heavy setup (load weights; a windowed
       analyzer also primes its recent-frame window off the store here).
    2. Pick the store iterator by ``analyzer.windowed`` and set the manager's ``total``:
       - stateless (YOLO) → ``store.iter_unanalyzed(name)`` over just the frames lacking a
         verdict, so a re-run resumes cheaply and skips done work; ``total`` is that count.
       - windowed (BSUV) → ``store.iter_time_order()`` over the *full* set in time order,
         because its verdict depends on temporal neighbours and it must revisit every
         frame; ``total`` is the whole store count.
    3. For each ``(frame_id, abs_path)``: read the JPEG bytes off disk, ``cv2.imdecode`` to
       a BGR ndarray, ``analyzer.analyze`` it, and ``store.write_analysis`` the verdict;
       then advance the manager's ``done`` (and ``present`` when the verdict is True).
    4. A read/decode/inference error on ONE frame is logged (throttled) and skipped — just
       like the collector's per-frame ``store.add`` failure handling — so a single corrupt
       or just-evicted frame can never abort a long sweep. A *fatal* error (``prepare``
       failed, the iterator itself broke) propagates to the worker (``_run``), which
       records it into ``status().error``.

    ``manager.stop_event`` is checked between frames, so a cancel takes effect promptly at
    the next frame boundary rather than mid-inference. The frame count that drives that
    verdict is stable for the job's lifetime: only one sweep runs at a time, so the store's
    concurrent inserts/evictions never reshuffle a stateless run's cursor (see the store
    iterators' keyset discipline).
    """
    # Lazy CV imports (see module docstring): kept out of module import so the API layer
    # can hold an AnalysisManager without touching the CV stack. Done once here, before
    # the loop, not per frame.
    import cv2
    import numpy as np

    analyzer.prepare(store)

    # Reanalyze clears the analyzer's prior verdicts only AFTER prepare() succeeds —
    # never before — so a prepare that fails (missing deps, no CUDA, model not wired)
    # leaves the old verdicts intact rather than wiping them for a sweep that never runs.
    if reanalyze:
        store.clear_analysis(analyzer.name)

    # Snapshot the frame horizon so the progress denominator matches exactly the frames
    # this pass will visit: frames the collector inserts after now get id > until_id and
    # are out of scope (the next sweep picks them up), so ``done`` can never overrun
    # ``total`` while ingest keeps running.
    until_id = store.latest_id()
    if analyzer.windowed:
        # Windowed oracle: drive the whole store in time order so its rolling
        # recent-background window stays contiguous; the denominator is the snapshot count.
        iterator = store.iter_time_order(until_id=until_id)
        total = store.stats()["count"]
    else:
        # Stateless oracle: drive only the un-verdicted frames so a re-run is cheap; the
        # denominator is just that outstanding TODO count, capped to the snapshot horizon.
        iterator = store.iter_unanalyzed(analyzer.name, until_id=until_id)
        total = store.count_unanalyzed(analyzer.name, until_id=until_id)
    manager.set_total(total)

    logger.info(
        "analysis sweep started: analyzer=%s windowed=%s total=%d",
        analyzer.name,
        analyzer.windowed,
        total,
    )

    done = 0
    errors = 0
    for frame_id, abs_path in iterator:
        if manager.stop_event.is_set():
            logger.info("analysis sweep canceled: analyzer=%s after %d verdicts", analyzer.name, done)
            break
        try:
            with open(abs_path, "rb") as fh:
                buf = fh.read()
            image = cv2.imdecode(np.frombuffer(buf, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                # imdecode returns None (not an exception) on a truncated/corrupt JPEG;
                # raise so it lands in the same log-and-skip path as a read/inference error.
                raise ValueError(f"failed to decode JPEG at {abs_path!r}")
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

    logger.info("analysis sweep finished: analyzer=%s %d verdicts, %d skipped", analyzer.name, done, errors)


class AnalysisManager:
    """Owns the single active analysis job and serializes it (one at a time).

    Mirrors the collector's daemon-thread + stop-event shape (see
    ``compute/collection/collector.py`` and how ``create_app`` drives it): a background
    daemon thread runs ``run_analysis``; a ``threading.Event`` cancels it between frames; a
    ``threading.Lock`` guards the mutable state (``running`` / counters / ``error``) so the
    API's status poll and the worker thread never race. Only ONE job runs at once —
    ``start`` refuses a second while one is live — because the store's single connection and
    the GPU are shared and the progress counters assume a single sweep.

    ``resolver`` is the injection seam: it defaults to the package registry
    ``get_analyzer`` but a test can pass a stub returning a fake analyzer, so the manager's
    threading/lifecycle can be exercised with no real model and none of its heavy deps.
    """

    def __init__(self, resolver=get_analyzer) -> None:
        self._resolver = resolver
        # One lock guards every field below; taken briefly for reads (status) and writes
        # (start / set_total / record / _run's finally), NEVER held across the inference.
        self._lock = threading.Lock()
        self._running = False
        self._analyzer: "str | None" = None
        self._done = 0
        self._total = 0
        self._present = 0
        self._error: "str | None" = None
        # Replaced with a fresh Event on every ``start`` so a prior job's set flag can't
        # pre-cancel the next one; the worker reads it directly between frames, which is
        # safe because ``running`` forbids overlapping jobs from ever reassigning it mid-run.
        self.stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self, store: "Store", name: str, reanalyze: bool = False) -> None:
        """Resolve a registered oracle by ``name`` and launch a sweep; refuse if one runs.

        The name-based entry point for the fixed oracles (yolo/bsuv): it resolves the
        backend through ``self._resolver`` (a bad name raises ``ValueError`` → 400, and
        an ``ImportError`` from a backend whose optional deps are absent propagates → 503
        with the install hint), then delegates to ``start_analyzer``, which does the
        launch under the lock. Resolution is a pure construction with no heavy import
        (see ``get_analyzer``) and no manager side effects, so doing it before the lock
        is harmless — the atomicity that matters (dep check + counter reset + the
        ``running`` flip) lives in ``start_analyzer``.

        ``reanalyze`` rides through to the worker unchanged (see ``start_analyzer`` /
        ``run_analysis``). Behavior for the registered oracles is exactly as before —
        this method is now a thin wrapper so that ``start_analyzer`` can also run a
        *pre-constructed* analyzer the registry can't build (a parameterized
        ``MogAnalyzer`` tuning run).
        """
        analyzer = self._resolver(name)
        self.start_analyzer(store, analyzer, reanalyze=reanalyze)

    def start_analyzer(self, store: "Store", analyzer: "Analyzer", reanalyze: bool = False) -> None:
        """Launch a sweep for an ALREADY-CONSTRUCTED analyzer instance; refuse if one runs.

        The instance-based core ``start`` delegates to, and the seam the tuning path uses
        directly: a ``MogAnalyzer(params, slot)`` needs params from the run request, which
        the name→instance registry can't supply, so it is constructed by the caller and
        handed here without any ``ANALYZER_NAMES`` entry.

        ``analyzer.ensure_available()`` runs SYNCHRONOUSLY here — before the thread is
        spawned, while the HTTP handler is still on the stack — so a backend whose optional
        deps/hardware are absent surfaces to the caller as ``ImportError`` (→ 503) rather
        than vanishing into the worker as a delayed ``status().error``. ``status()['analyzer']``
        is set to ``analyzer.name`` (e.g. ``'mog2:candidate'``), the instance's own id, so
        the poll reports which run is live.

        ``reanalyze`` rides along to the worker, where the verdict clear happens only after
        a successful ``prepare`` (see ``run_analysis``), so a deps-missing run can't wipe
        verdicts here. Raising ``RuntimeError`` when one already runs is what the API maps to
        a 409. The dep check + counter reset + the ``running`` flip all happen under the lock:
        if any raises, no job started and ``running`` stays False. The dep check is
        imports-only (no weights load), so holding the lock across it is negligible and buys
        atomicity against a racing second start.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("analysis already running")
            analyzer.ensure_available()
            self._analyzer = analyzer.name
            self._done = 0
            self._total = 0
            self._present = 0
            self._error = None
            self.stop_event = threading.Event()
            self._running = True
            thread = threading.Thread(
                target=self._run, args=(store, analyzer, reanalyze), name="analysis", daemon=True
            )
            self._thread = thread
        # Start outside the lock so a concurrent status() poll isn't blocked by thread spin-up.
        thread.start()

    def _run(self, store: "Store", analyzer: "Analyzer", reanalyze: bool = False) -> None:
        """Worker body: run the sweep, capture any fatal error, always clear ``running``.

        Per-frame failures are handled inside ``run_analysis`` (log-and-skip); anything
        that reaches here is fatal to the job (``prepare`` failed, the store iterator broke)
        — recorded into ``error`` for the status poll and logged. ``running`` is cleared in
        a ``finally`` no matter what, so a crashed job never wedges the manager into a
        permanently-busy state that would refuse every future ``start``. ``reanalyze`` is
        forwarded so the (post-prepare) verdict clear happens on this thread.
        """
        try:
            run_analysis(store, analyzer, self, reanalyze)
        except Exception as exc:
            logger.exception("analysis sweep failed")
            with self._lock:
                self._error = str(exc)
        finally:
            with self._lock:
                self._running = False

    def cancel(self) -> None:
        """Request cancellation; the worker stops at the next frame boundary.

        Idempotent and safe to call when idle — it just sets the current Event, which the
        next ``start`` replaces. Does not block for the thread to finish; the caller polls
        ``status().running`` to watch it wind down.
        """
        self.stop_event.set()

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

    def status(self) -> dict:
        """A consistent snapshot of the job state for the ``/api/analysis/status`` poll."""
        with self._lock:
            return {
                "running": self._running,
                "analyzer": self._analyzer,
                "done": self._done,
                "total": self._total,
                "present": self._present,
                "error": self._error,
            }

    @property
    def running(self) -> bool:
        """Whether a sweep is currently active (lock-guarded read)."""
        with self._lock:
            return self._running

"""The always-on YOLO-oracle worker — keeps full-coverage oracle verdicts pre-computed.

Tuning the edge's MOG2 motion gate needs a *trusted oracle* over the frames it is scored
against, and — critically — over the **non-motion** frames too: a gate MISS is a frame the
gate called still that in fact held a cat, so it is only visible where a non-motion frame
was both stored and swept. Producing that coverage has meant picking a day on the Motion-
tuning page and waiting on a manual ``yolo-serial`` sweep before any scorecard could be
read. This worker removes that wait by sweeping the tail continuously as frames arrive, so
a day captured while it was on is already ~100% covered.

Two pieces, mirroring ``compute/learning/live_identify.py`` (itself mirroring
``compute/collection/collector.py``'s ``run_collector`` + ``CollectorManager`` pairing):

- ``run_yolo_oracle`` — the daemon loop: sleep ``tick_seconds`` on an interruptible
  ``stop_event.wait`` (so a stop is near-instant, not up to a full interval late), then run
  one tick, until stopped. The tick body is ``YoloOracleManager._tick`` so it can be driven
  exactly ONCE from a test.
- ``YoloOracleManager`` — the runtime start/stop control: an authoritative ``running``
  intent flag (NOT derived from ``thread.is_alive()``), a fresh ``(thread, stop_event)``
  per ``start``, a best-effort join of any prior thread, a persisted on/off intent + frame
  watermark, and a ``status()`` snapshot for ``/api/stats``.

**How it differs from the live-identify worker**, which is otherwise its twin:

- **Detect-only.** It runs ``yolo-serial`` detection and stops there — no embedding, no
  gallery, no ``identifications`` rows. So it needs no promoted model and does useful work
  from day one, where live-identify idles until something is promoted.
- **Full coverage, not visit spans.** Live-identify detects only *closed motion clusters*
  (which is exactly why a live-populated window is NOT tunable — see changelog 76). This
  worker sweeps every id in the tail, motion and non-motion alike, which is what makes the
  coverage uniform enough to score a gate against.
- **Idles in motion-only capture.** With motion-only capture on, the non-motion frames are
  never stored, so full coverage is both impossible and pointless; the tick no-ops (intent
  preserved) until capture returns to keep-all.

**It never backfills.** Every ``start`` — an operator switch-on *and* the launch-time
``restore`` — seeds the watermark to the current frame horizon, so the worker only ever
covers frames captured from that moment on. Frames stored before it was switched on (earlier
days, or a gap while it was off) stay the manual sweep's job. This is deliberate: a backfill
is an unbounded, hours-long GPU hold that starts the moment the toggle flips and silently
delays coverage of *today* — the thing the worker exists to keep current. Catch-up WITHIN a
run is unaffected: a tick that yielded to a manual job (or idled under motion-only capture)
leaves the watermark alone and drains the tail on later ticks.

**The two always-on YOLO loops deliberately do NOT yield to each other.** Both yield to a
manual sweep/training job (``is_busy``), but neither waits on the other: a same-frame
detect is idempotent (``analysis`` is ``PRIMARY KEY (frame_id, analyzer)`` written
``INSERT OR REPLACE``, and every write serializes on the store lock), so an occasional
overlap wastes a little GPU work and can never corrupt. Feeding this worker's ``running``
into live-identify's ``is_busy`` would have been *wrong*, not merely conservative:
``running`` is an INTENT flag (true the whole time the toggle is on, not just while a tick
is in flight), unlike the FIFO managers' active-job flags — so it would have suppressed
live naming for as long as this worker was enabled.

Like ``live_identify``, everything touching torch / the GPU — the detect callable
(``run_analysis``), the ``yolo-serial`` analyzer factory, and the clock — is a constructor
argument, so the whole tick / threading / lifecycle is exercisable with fakes on the
GPU-less dev box. Importing this module stays torch-free: the default analyzer factory is a
lambda, so ``get_analyzer("yolo-serial")`` is not called until a tick actually runs.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from compute.analysis import get_analyzer
from compute.analysis.runner import DetectAdapter, run_analysis

if TYPE_CHECKING:
    from typing import Callable

logger = logging.getLogger(__name__)

# Default seconds between ticks. Deliberately much longer than live-identify's 5 s: that
# worker is latency-sensitive (a visit should appear *named* shortly after it ends), while
# oracle coverage is not — nothing reads it until an operator opens a scorecard. A longer
# interval keeps up with ~5 fps capture trivially (~150 frames/tick) while cutting the
# per-tick overhead (an ``analyzer.prepare`` re-check and one "sweep started" log line per
# chunk) roughly six-fold.
_DEFAULT_TICK = 30.0

# Most frames one tick will sweep before deferring the rest to the next interval. Bounds a
# single tick's GPU hold: after a manual job (or a spell of motion-only capture) held the tick
# off for a while, the tail beyond the watermark can be large, and sweeping all of it
# back-to-back would monopolize the GPU far past one tick. Capping drains that backlog
# GRADUALLY across ticks. (Frames from before the worker was switched on are never in the
# window at all — start() seeds the watermark to the horizon, so there is no backfill to cap.)
_MAX_FRAMES_PER_TICK = 2000

# How many frame ids one ``run_analysis`` call covers — i.e. the tick's YIELD GRANULARITY.
# ``run_analysis`` honors only ``stop_event``, never ``is_busy``, so a manual job that
# arrives mid-tick cannot interrupt a call in progress; it wins at the next chunk boundary.
# Hence the tick walks its window in chunks and re-checks between them, instead of issuing
# one monolithic ``[since, until]`` call that would hold the GPU for the whole cap.
_CHUNK = 256

# How long ``start`` waits for a just-stopped worker thread to actually exit before
# spawning its replacement. Same rationale as the collector's and live-identify's constant:
# a thread told to stop is usually parked in ``stop_event.wait`` (or mid-detect) and notices
# the flag only at the next boundary, so we wait a little but never indefinitely, so a rapid
# stop→start can't wedge the HTTP handler that called ``start``.
_STOP_JOIN_TIMEOUT_S = 2.0

# The analyzer this worker sweeps with. It MUST be the serial persona: the batched ``yolo``
# path over-detects relative to it, so ``yolo-serial`` is the trusted oracle and the one the
# scorecard/annotation paths read. Sweeping with anything else would silently poison the
# very verdicts this worker exists to pre-compute.
_ANALYZER = "yolo-serial"

# Settings-KV keys: the on/off intent (restored at launch, like the collector's motion-only
# intent) and the frame watermark. The watermark bounds the tick to new frames;
# ``iter_unanalyzed`` (keyed on absence of a verdict row) makes the sweep itself idempotent
# WITHIN that window. Persisting the watermark keeps ``status()`` honest between the process
# start and the launch-time ``restore`` (which re-seeds it to the horizon — no backfill); a
# watermark left AHEAD of the frames it should cover strands a RUNNING worker, which is why
# ``/api/clear`` re-seeds it too (``clear()`` keeps this KV while frame rowids reset).
_INTENT_KEY = "yolo_oracle"
_WATERMARK_KEY = "yolo_oracle_watermark"


def _default_now_ms() -> int:
    """Wall-clock milliseconds — the injectable clock's real default (tests pass a fake)."""
    return int(time.time() * 1000)


def run_yolo_oracle(
    manager: "YoloOracleManager",
    stop_event: threading.Event,
    tick_seconds: float,
) -> None:
    """Daemon loop: wait ``tick_seconds`` (interruptibly), run one tick, until stopped.

    The timing shell around ``manager._tick`` — the direct analogue of
    ``run_live_identify``. ``stop_event.wait`` returns True the instant the event is set, so
    a stop breaks the wait immediately rather than after the remaining interval; a timeout
    (False) runs exactly one tick. ``_tick`` owns all error survival (it records
    ``last_error`` and stops the tick without advancing the watermark past a failure), so
    the loop itself stays a plain wait-then-tick — but a defensive guard here still keeps a
    wholly-unexpected tick fault from ever leaving the worker dead with ``running=True``.
    """
    logger.info("yolo-oracle worker started (tick=%.1fs)", tick_seconds)
    while not stop_event.is_set():
        if stop_event.wait(tick_seconds):
            break
        try:
            manager._tick(stop_event)
        except Exception:  # pragma: no cover - _tick already contains its own guard
            logger.exception("yolo-oracle: tick crashed unexpectedly")
    logger.info("yolo-oracle worker stopped")


class YoloOracleManager:
    """Runtime start/stop control for the always-on full-coverage YOLO sweep.

    Shaped like ``LiveIdentifyManager`` / ``CollectorManager``: it owns the single active
    run's ``(thread, stop_event)`` plus an authoritative ``running`` flag set
    *synchronously* in ``start``/``stop`` — deliberately NOT derived from
    ``thread.is_alive()`` (the API contract is "the route toggles and reports the resulting
    state", and liveness is a poor proxy: a worker mid-``wait`` looks alive long after
    ``stop`` asked it to quit). The flag tracks *intent*; the daemon winds down on its own
    schedule at the next boundary. **That intent semantics is why no other worker may read
    this flag as a GPU-busy signal** — see the module docstring.

    The heavy detect runs OUTSIDE the manager lock, on the worker thread; the lock guards
    only the small ``(thread, stop_event, running, watermark, last_tick_ts, last_error)``
    bookkeeping so a ``status()`` poll and a concurrent ``start``/``stop`` never see a torn
    snapshot. The resident analyzer is touched only by the worker thread, so it needs no lock.

    ``is_busy`` is a zero-arg predicate — True while a manual analysis/training job runs —
    that the tick consults to yield the GPU. ``motion_only`` is a zero-arg getter for the
    CURRENT capture mode, read fresh each tick (like ``run_collector``'s) so an operator's
    mid-run flip takes effect without touching this worker's intent. ``detect`` /
    ``analyzer_factory`` / ``now_ms`` are the injection seams: a test passes fakes so
    nothing here touches torch or the GPU.
    """

    def __init__(
        self,
        store,
        is_busy: "Callable[[], bool]",
        motion_only: "Callable[[], bool]" = (lambda: False),
        *,
        detect=run_analysis,
        analyzer_factory: "Callable[[], object]" = (lambda: get_analyzer(_ANALYZER)),
        tick_seconds: float = _DEFAULT_TICK,
        now_ms: "Callable[[], int]" = _default_now_ms,
    ) -> None:
        self._store = store
        self._is_busy = is_busy
        self._motion_only = motion_only
        self._detect = detect
        self._analyzer_factory = analyzer_factory
        self._tick_seconds = tick_seconds
        self._now_ms = now_ms

        # Guards the (thread, stop_event, running) triple AND the status fields (watermark,
        # last_tick_ts, last_error). Held only for quick bookkeeping — never across a tick's
        # detect, which runs on the worker thread and touches only the (separately locked)
        # store.
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None
        self._stop_event: "threading.Event | None" = None
        self._running = False

        # Worker-thread-only resident state: the yolo-serial analyzer, built once and reused
        # across chunks/ticks so no chunk reloads weights (``run_analysis`` re-calls
        # ``prepare()``, which is idempotent). Never read by status(), so no lock.
        self._analyzer: "object | None" = None

        # The frame watermark: the highest id already swept, so a tick only sweeps beyond it.
        # Loaded from the persisted setting purely so ``status()`` reports the last covered
        # frame before the worker is (re)started — every ``start`` re-seeds it to the horizon,
        # so this value never causes a backfill (0 = nothing swept yet).
        raw = store.get_setting(_WATERMARK_KEY)
        self._watermark = int(raw) if raw is not None else 0

        # Bumped by every watermark re-seed (``start``'s horizon seed and ``reset_watermark``).
        # A tick snapshots it and refuses to advance the watermark once it has changed, because
        # a re-seed can land mid-flight (``/api/clear`` does not stop the workers; a stop→start
        # leaves the old thread winding down): the tick computed its window from the PREVIOUS
        # watermark and would otherwise write its own derived value back, resurrecting a stale
        # value — with ``/api/clear`` one far above every post-wipe frame, leaving the worker
        # reporting running and error-free while silently sweeping nothing forever.
        self._epoch = 0

        # Observability for /api/stats: when the last tick ran, and the most-recent tick
        # error (sticky until the next error, so a returning operator still sees it).
        self._last_tick_ts: "int | None" = None
        self._last_error: "str | None" = None

    # --- The tick ------------------------------------------------------------------------

    def _tick(self, stop_event: threading.Event) -> None:
        """Sweep one bounded slice of the un-swept tail with ``yolo-serial``.

        Steps:

        1. If ``is_busy()`` a manual analysis/training job holds the GPU — skip, leaving the
           watermark untouched so the tick simply resumes next interval. Operator work
           always wins.
        2. If ``motion_only()`` the non-motion frames aren't being stored, so full coverage
           is unattainable — idle without touching the watermark. (Enforced here, not just
           by greying the UI toggle, so the invariant holds however the flag was flipped.)
        3. Build the ``yolo-serial`` analyzer once (reused across chunks and ticks).
        4. Resolve the window: ``since = watermark + 1`` through ``until =
           min(store.latest_id(), watermark + _MAX_FRAMES_PER_TICK)``. Nothing new → return.
        5. Walk ``[since, until]`` in ``_CHUNK``-sized sub-windows. Before each, re-check
           stop / ``is_busy`` and bail immediately if either fired — this is the tick's yield
           granularity, since ``run_analysis`` itself only honors ``stop_event``. Each chunk
           is a full-coverage detect (``motion_only`` left at its ``run_analysis`` default of
           False), so motion AND non-motion frames get a verdict; ``iter_unanalyzed`` inside
           skips any already done (e.g. a motion frame the live-identify worker raced to
           first).
        6. Advance + persist the watermark to a chunk's ``hi`` only AFTER that chunk's detect
           returned without a pending stop — a stop mid-detect makes ``run_analysis`` return
           normally with the chunk half-swept, so advancing then would strand its tail
           permanently un-swept. Parking at the last COMPLETED chunk lets the next run redo
           only the interrupted one.

        The whole body runs under one ``try``: a fault is logged, recorded into
        ``last_error``, and stops the tick WITHOUT advancing past the failed chunk — the
        worker thread stays alive and the next tick retries, mirroring the collector's
        per-frame error survival. All heavy work is outside the lock; only the small status
        writes take it.
        """
        now = self._now_ms()
        with self._lock:
            self._last_tick_ts = now

        try:
            if self._is_busy():
                return  # a manual analysis/training job owns the GPU — yield, watermark untouched
            if self._motion_only():
                return  # motion-only capture: no non-motion frames to cover — idle

            if self._analyzer is None:
                # Built once and reused every chunk/tick; run_analysis.prepare() is idempotent.
                self._analyzer = self._analyzer_factory()

            detect_manager = DetectAdapter(stop_event)
            # Snapshot the watermark AND the epoch together under the lock, so the window and
            # the staleness check agree; latest_id() is store I/O and stays outside the lock.
            with self._lock:
                epoch, watermark = self._epoch, self._watermark
            latest = self._store.latest_id()

            if watermark > latest:
                # Frame ids REGRESSED below the watermark. ``frames.id`` is INTEGER PRIMARY KEY
                # with NO AUTOINCREMENT, so SQLite REUSES rowids once the max row is deleted —
                # and the non-motion purge deletes through the current max id, which at ~5 fps
                # continuous capture is almost always a non-motion frame. Left alone the window
                # would be empty (``until < start``) on EVERY later tick, so the worker would
                # report running and error-free while sweeping nothing until the store regrew
                # past the stale value — hours. Clamping lets coverage self-heal next tick.
                logger.warning(
                    "yolo-oracle: frame ids regressed (watermark %d > latest %d) — clamping",
                    watermark,
                    latest,
                )
                if not self._advance_watermark(latest, epoch):
                    return
                watermark = latest

            start = watermark + 1
            until = min(latest, watermark + _MAX_FRAMES_PER_TICK)
            if until < start:
                return  # no new frames beyond the watermark

            lo = start
            while lo <= until:
                if stop_event.is_set() or self._is_busy():
                    # Re-check per chunk, not just once at tick start: a stop OR a manual job
                    # can arrive mid-tick and the shared GPU means yielding promptly beats
                    # finishing the window. Watermark stays at the last completed chunk.
                    break
                hi = min(lo + _CHUNK - 1, until)
                self._detect(self._store, self._analyzer, detect_manager, since_id=lo, until_id=hi)
                if stop_event.is_set():
                    # detect returns NORMALLY (not raising) when a stop aborts it between
                    # batches, leaving [lo, hi] only partly swept. Do NOT advance past a
                    # partial chunk — bail so the next run re-sweeps it whole.
                    break
                # Advance ONLY after this chunk completed, so a failure above never lets the
                # watermark skip un-swept frames. A False return means a reset landed
                # mid-tick, so this window is stale — abandon it rather than write a
                # pre-wipe-derived watermark.
                if not self._advance_watermark(hi, epoch):
                    break
                lo = hi + 1
        except Exception as exc:
            # A per-chunk (or setup) fault must not kill the always-on worker: log it,
            # surface it on status().last_error, and stop the tick here — the watermark is
            # already parked at the last good chunk, so the next tick simply retries.
            logger.exception("yolo-oracle: tick failed")
            with self._lock:
                self._last_error = str(exc)

    def _advance_watermark(self, value: int, epoch: int) -> bool:
        """Set + persist the watermark, unless a ``reset_watermark`` landed since ``epoch``.

        Returns False when the epoch has moved — i.e. ``/api/clear`` re-seeded the watermark
        while this tick was in flight, so the tick's window was computed against the PRE-wipe
        store. Writing a value derived from it would resurrect a watermark above every
        post-wipe frame and silently strand the worker (the exact failure ``reset_watermark``
        exists to prevent), so the caller must abandon the tick instead. In-memory first, then
        persisted: a crash between the two costs re-work, never lost coverage.
        """
        with self._lock:
            if epoch != self._epoch:
                return False
            self._watermark = int(value)
        self._store.set_setting(_WATERMARK_KEY, str(int(value)))
        return True

    # --- Lifecycle (mirrors LiveIdentifyManager) ------------------------------------------

    def start(self) -> None:
        """Start the worker; idempotent, and persists the on intent.

        A stopped run is replaced by a FRESH thread + stop event (both one-shot). Before
        spawning the replacement we best-effort join any prior thread (bounded by
        ``_STOP_JOIN_TIMEOUT_S``) so a rapid stop→start doesn't leave the previous worker
        briefly overlapping on the shared GPU; if the join times out we proceed anyway (the
        old thread carries the old, set stop event and exits at its next boundary). The
        intent is persisted so a launch can restore it (see ``restore``).

        **The watermark is seeded to the current frame horizon on EVERY start**, so the worker
        never backfills: it covers frames captured from the switch-on forward, and everything
        older — earlier days, or the gap while it was off — stays the manual sweep's job. A
        backfill would be an unbounded, hours-long GPU hold that delays the coverage of *today*
        this worker exists to keep current. Note this applies to the launch-time ``restore``
        too, which is harmless: frames are only stored by the in-process collector, so a
        process that was down stored nothing to miss.
        """
        # Seed BEFORE (and outside) the lock — it reads/writes the store, which must never
        # happen under the manager lock. Skipped when already running so an idempotent start()
        # can't skip the tail a live worker is mid-way through draining; the authoritative
        # not-running check is the locked one below. Two racing starts both seed, harmlessly:
        # they resolve within milliseconds of each other, and the loop waits a full tick
        # interval BEFORE its first tick, so no sweep can have advanced the watermark in
        # between — both write the same horizon.
        if self.running:
            return
        self._seed_watermark(self._store.latest_id())
        with self._lock:
            if self._running:
                return
            stale = self._thread
            stop_event = threading.Event()
            thread = threading.Thread(
                target=run_yolo_oracle,
                args=(self, stop_event, self._tick_seconds),
                name="yolo-oracle",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            self._running = True
        # Join the previous thread and start the new one OUTSIDE the lock (a status poll must
        # not stall behind the join); persist intent via the store's own lock, never nested
        # inside the manager lock.
        if stale is not None and stale.is_alive():
            stale.join(timeout=_STOP_JOIN_TIMEOUT_S)
        thread.start()
        self._store.set_setting(_INTENT_KEY, "1")

    def stop(self, persist: bool = True) -> None:
        """Stop the worker; idempotent. Signals the loop and flips ``running``.

        Sets the current stop event and clears ``running`` synchronously so the next
        ``/api/stats`` poll sees "stopped" at once. It does NOT join — the thread may be
        parked in ``stop_event.wait`` or mid-detect and would only notice at the next
        boundary; the daemon winds down on its own, and ``start`` handles any leftover before
        spawning a replacement. ``join`` (below) is the shutdown-only wait.

        ``persist`` distinguishes an OPERATOR stop from a PROCESS stop, and getting it wrong
        breaks the restore contract: the shutdown hook also calls ``stop()``, so persisting
        "0" unconditionally would clear the operator's on-intent on every clean exit and the
        worker could NEVER restore at the next launch. The route passes the default (a
        deliberate off, remembered); ``_shutdown`` passes ``persist=False`` (a process exit,
        intent preserved). Same split the collector makes by persisting its intent in the
        route rather than the manager.
        """
        with self._lock:
            if not self._running:
                return
            if self._stop_event is not None:
                self._stop_event.set()
            self._running = False
        if persist:
            self._store.set_setting(_INTENT_KEY, "0")

    def restore(self, flag: bool) -> None:
        """Start the worker iff the persisted intent was on — the launch-time restore.

        Mirrors ``LiveIdentifyManager.restore``: called by ``create_app`` on a live app with
        ``store.get_setting("yolo_oracle") == "1"``. A falsy flag is a no-op (stay stopped);
        a truthy flag goes through ``start`` (which itself persists "1" again — harmless).

        Unlike live-identify's restore there is deliberately no "…or a model is promoted"
        clause: a promoted gallery is a run-mode *naming* signal, whereas this worker is a
        motion-gate *tuning* tool, so it starts only when the operator asked for it.

        Going through ``start`` means a restore also re-seeds the watermark to the horizon, so a
        restart does not backfill either — see ``start``. Nothing is lost: frames are stored by
        the in-process collector, so a process that was down stored none.
        """
        if flag:
            self.start()

    def reset_watermark(self, value: int) -> None:
        """Re-point the watermark (and its persisted copy) at ``value`` — for ``/api/clear``.

        ``Store.clear()`` wipes the frames but deliberately KEEPS the settings KV, while
        frame rowids restart from 1. A watermark left at the pre-wipe horizon would then sit
        far AHEAD of every new frame, so the tick's ``[watermark+1, …]`` window would never
        include them and a RUNNING worker would look enabled while silently covering nothing —
        until the store re-grew past the old id. ``iter_unanalyzed`` cannot save us here: it
        is only consulted *inside* the window, so frames below it are never even considered.
        Re-seeding to the post-wipe horizon restores the normal "cover what arrives from now
        on" contract. (A *stopped* worker would be re-seeded by its next ``start`` anyway; this
        is what covers the clear-while-running case, since ``/api/clear`` stops nothing.)

        Load-bearing detail: the ``_seed_watermark`` epoch bump means a tick already in flight
        refuses to write its pre-wipe-derived watermark back over this reset — without that
        guard the reset is silently undone and the worker strands exactly as described above.
        See ``_advance_watermark``.
        """
        self._seed_watermark(value)

    def _seed_watermark(self, value: int) -> None:
        """Force the watermark (and its persisted copy) to ``value``, invalidating in-flight ticks.

        The one write path for a watermark that does NOT come from a completed sweep — the
        horizon seed in ``start`` and ``/api/clear``'s ``reset_watermark``. Bumping ``_epoch``
        is what makes it stick: a tick that is mid-chunk computed its window from the previous
        watermark, so ``_advance_watermark`` must refuse its derived value rather than undo
        this. In-memory first, then persisted, like ``_advance_watermark``: a crash between the
        two costs at most a little re-work. The store write stays OUTSIDE the manager lock.
        """
        with self._lock:
            self._epoch += 1
            self._watermark = int(value)
        self._store.set_setting(_WATERMARK_KEY, str(int(value)))

    def join(self, timeout: "float | None" = None) -> None:
        """Best-effort wait for the worker thread to exit — for shutdown only.

        ``stop()`` deliberately doesn't join (it must not stall an HTTP handler). At process
        exit, though, the store's connection is about to close and the worker writes through
        it (``set_setting``, and the detect pass's verdict writes), so pair ``stop()`` with
        this ``join`` so an in-flight tick finishes before ``store.close()`` rather than
        racing a closed DB. The thread parks in ``stop_event.wait`` and notices the stop at
        the next boundary, so pass a ``timeout`` to bound how long exit waits; it is a daemon
        and dies with the process regardless. Snapshot the reference under the lock, join
        OUTSIDE it.
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def running(self) -> bool:
        """Whether the worker is currently on (lock-guarded read of the INTENT flag).

        Intent, not activity: true for the whole time the toggle is on, including between
        ticks and while a tick is idling for motion-only capture. Never use it as a
        "GPU busy" signal (see the module docstring).
        """
        with self._lock:
            return self._running

    def status(self) -> dict:
        """A consistent snapshot for the ``/api/stats`` poll (lock-guarded).

        ``running`` is the intent flag; ``watermark`` is the highest frame id swept;
        ``last_tick_ts`` is when the last tick ran (``None`` before the first); and
        ``last_error`` is the most-recent tick error (``None`` if none yet, sticky until the
        next error so a returning operator still sees it).
        """
        with self._lock:
            return {
                "running": self._running,
                "watermark": self._watermark,
                "last_tick_ts": self._last_tick_ts,
                "last_error": self._last_error,
            }

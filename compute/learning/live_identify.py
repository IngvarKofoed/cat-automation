"""The always-on live-identification worker — names NEW visits without a manual pass.

Where the manual Identify pass (``TrainingManager`` job) sweeps the whole store on
an operator click, this is the compute analogue of the *collector*: a single
always-on background loop that, every few seconds, looks at the settled tail of
collected frames and — for each *closed* motion cluster (the same unit
``events()`` renders as an Activity event) — runs ``yolo-serial`` detection then
``run_identify`` against the ACTIVE gallery, so a visit shows up *named* in
Activity within ~one tick of ending. ``events()`` is untouched: it already names a
cluster from whatever ``identifications`` rows fall in its span; the worker just
keeps those rows populated as visits happen (see the live-identify-worker spec).

Two pieces, mirroring ``compute/collection/collector.py``'s ``run_collector`` +
``CollectorManager`` pairing:

- ``run_live_identify`` — the daemon loop: sleep ``tick_seconds`` on an
  interruptible ``stop_event.wait`` (so a stop is near-instant, not up to a full
  interval late), then run one tick, until stopped. The tick body itself is
  ``LiveIdentifyManager._tick`` so it can be driven exactly ONCE from a test.
- ``LiveIdentifyManager`` — the runtime start/stop control, shaped like
  ``CollectorManager``: an authoritative ``running`` intent flag (NOT derived from
  ``thread.is_alive()``), a fresh ``(thread, stop_event)`` per ``start``, a
  best-effort join of any prior thread, a persisted on/off intent + a persisted
  frame watermark, and a ``status()`` snapshot for ``/api/stats``.

Why a dedicated always-on loop rather than a job on the ``AnalysisManager`` /
``TrainingManager`` FIFOs: those are one-shot walk-away job lists, and an endless
re-enqueue would pollute their history and contend on the same queue slot as the
operator's manual gallery-build / validate. This loop instead *yields* the GPU
whenever such a manual job runs (``is_busy``) — the GPU and the single SQLite
connection are shared, so serial is the only safe execution and operator work
always wins; the watermark is untouched on a yielded tick, so it simply resumes
next interval.

**Injectability is load-bearing.** Everything that touches torch / the GPU — the
detect callable (``run_analysis``), the identify callable (``run_identify``), the
``yolo-serial`` analyzer factory, the ``Embedder`` factory, and even the clock
(``now_ms``) — is a constructor argument, so the whole tick / threading /
lifecycle is exercisable with fakes on the GPU-less dev box (mirroring how
``TrainingManager`` takes ``identifier=run_identify``). Importing this module stays
torch-free: ``run_identify`` / ``Embedder`` reach torch only through their own
lazy-imported methods, and the default analyzer factory is a lambda so
``get_analyzer("yolo-serial")`` is not called until a tick actually runs.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from compute.analysis import get_analyzer
from compute.analysis.runner import run_analysis
from compute.identification.embed import EmbedCancelled, Embedder
from compute.identification.gallery import load_gallery, run_identify

if TYPE_CHECKING:
    from typing import Callable

logger = logging.getLogger(__name__)

# Default seconds between ticks. Latency from a visit ending to its name appearing is
# roughly ``_VISIT_GAP_MS`` (the cluster-closed wait, ~2 s) plus up to one tick — near-
# live, deliberately not sub-second (the visit must settle before it can be named).
_DEFAULT_TICK = 5.0

# Most closed-visit spans one tick will process before yielding to the next interval.
# Bounds a single tick's GPU hold: on a re-enable against a store that accreted many
# visits while the worker was off, closed_visits can return thousands of spans, and
# running detect+identify over all of them back-to-back would monopolize the GPU for
# far longer than a tick and starve is_busy re-checks. Capping drains a backlog GRADUALLY
# across ticks, re-checking stop/is_busy between each, instead of in one unbounded run.
# (A first-EVER enable skips history entirely by seeding the watermark to the horizon —
# see start(); this cap is for the resume-after-a-gap case.)
_MAX_SPANS_PER_TICK = 50

# How long ``start`` waits for a just-stopped worker thread to actually exit before
# spawning its replacement. Same rationale as the collector's constant: a thread told
# to stop is usually parked in ``stop_event.wait`` (or mid-detect) and notices the flag
# only at the next boundary, so we wait a little but never indefinitely, so a rapid
# stop→start can't wedge the HTTP handler that called ``start``.
_STOP_JOIN_TIMEOUT_S = 2.0

# Settings-KV keys: the on/off intent (restored at launch, like the collector's
# motion-only intent) and the frame watermark. The watermark is only an optimization —
# it avoids rescanning already-processed spans; the idempotent resume queries
# (``iter_unanalyzed`` / ``iter_unidentified``, both keyed on absence of a row) are the
# real correctness backstop, so a lost/stale watermark costs at most re-work, never a
# missed or double-counted visit.
_INTENT_KEY = "live_identify"
_WATERMARK_KEY = "live_identify_watermark"


def _default_now_ms() -> int:
    """Wall-clock milliseconds — the injectable clock's real default (tests pass a fake)."""
    return int(time.time() * 1000)


class _DetectAdapter:
    """The minimal ``manager`` stand-in ``run_analysis`` needs, for one tick's detect.

    ``run_analysis`` reads only three things off its ``manager``: ``.stop_event`` (an
    ``Event`` it polls between batches/frames to honor a cancel), ``.set_total(int)`` and
    ``.record(bool)`` (progress hooks). The live worker has no progress bar, so the two
    counter hooks are no-ops; the ``stop_event`` IS the worker's own, so a ``stop()``
    that sets it aborts an in-flight detect at the next batch boundary — the same reason
    the collector's ``stop`` reaches its loop.
    """

    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event

    def set_total(self, total: int) -> None:  # noqa: D401 - no-op progress hook
        pass

    def record(self, present: bool) -> None:  # noqa: D401 - no-op progress hook
        pass


def run_live_identify(
    manager: "LiveIdentifyManager",
    stop_event: threading.Event,
    tick_seconds: float,
) -> None:
    """Daemon loop: wait ``tick_seconds`` (interruptibly), run one tick, until stopped.

    The timing shell around ``manager._tick`` — the direct analogue of ``run_collector``
    (which loops the stream) but reading frames *out* of the store instead of pumping
    them in. ``stop_event.wait`` returns True the instant the event is set, so a stop
    breaks the wait immediately rather than after the remaining interval; a timeout
    (False) runs exactly one tick. ``_tick`` owns all per-span error survival (it records
    ``last_error`` and stops the tick without advancing the watermark past a failure), so
    the loop itself stays a plain wait-then-tick with nothing to catch — but a defensive
    guard here still keeps a wholly-unexpected tick fault from ever killing the daemon.
    """
    logger.info("live-identify worker started (tick=%.1fs)", tick_seconds)
    while not stop_event.is_set():
        if stop_event.wait(tick_seconds):
            break
        try:
            manager._tick(stop_event)
        except Exception:  # pragma: no cover - _tick already contains its own guard
            # _tick catches its own errors into last_error; this is a belt-and-braces
            # net so no path can leave the always-on worker dead with running=True.
            logger.exception("live-identify: tick crashed unexpectedly")
    logger.info("live-identify worker stopped")


class LiveIdentifyManager:
    """Runtime start/stop control for the always-on live-identification loop.

    Shaped like ``CollectorManager``: it owns the single active run's ``(thread,
    stop_event)`` plus an authoritative ``running`` flag set *synchronously* in
    ``start``/``stop`` — deliberately NOT derived from ``thread.is_alive()``, for the
    same reasons the collector gives (the API contract is "the route toggles and reports
    the resulting state", and liveness is a poor proxy — a worker mid-``wait`` looks
    alive long after ``stop`` asked it to quit). The flag tracks *intent*; the daemon
    thread winds down on its own schedule at the next ``wait`` boundary.

    Unlike the FIFO managers this is a loop, not a queue: no ``_pending``/``_history``,
    just the resident worker state (the prepared ``yolo-serial`` analyzer + the DINOv2
    ``Embedder``, held across ticks so a visit doesn't reload weights) and the persisted
    watermark. The heavy detect/identify run OUTSIDE the manager lock, on the worker
    thread; the lock guards only the small ``(thread, stop_event, running, watermark,
    last_tick_ts, last_error)`` bookkeeping so a ``status()`` poll and a concurrent
    ``start``/``stop`` never see a torn snapshot. The analyzer + embedder are touched
    only by the worker thread (never by ``status()``), so they need no lock.

    ``is_busy`` is a zero-arg predicate — True while a manual analysis/training job runs
    — that the tick consults to yield the GPU. ``detect`` / ``identify`` /
    ``analyzer_factory`` / ``embedder_factory`` / ``now_ms`` are the injection seams: a
    test passes fakes so nothing here touches torch or the GPU.
    """

    def __init__(
        self,
        store,
        is_busy: "Callable[[], bool]",
        *,
        detect=run_analysis,
        identify=run_identify,
        analyzer_factory: "Callable[[], object]" = (lambda: get_analyzer("yolo-serial")),
        embedder_factory: "Callable[..., object]" = Embedder,
        gallery_loader: "Callable[[str], object]" = load_gallery,
        tick_seconds: float = _DEFAULT_TICK,
        now_ms: "Callable[[], int]" = _default_now_ms,
    ) -> None:
        self._store = store
        self._is_busy = is_busy
        self._detect = detect
        self._identify = identify
        self._analyzer_factory = analyzer_factory
        self._embedder_factory = embedder_factory
        self._gallery_loader = gallery_loader
        self._tick_seconds = tick_seconds
        self._now_ms = now_ms

        # Guards the (thread, stop_event, running) triple AND the status fields
        # (watermark, last_tick_ts, last_error). Held only for quick bookkeeping — never
        # across a tick's detect/identify, which run on the worker thread and touch only
        # the (separately locked) store.
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None
        self._stop_event: "threading.Event | None" = None
        self._running = False

        # Worker-thread-only resident state, held across ticks so a visit reloads nothing:
        # the prepared yolo-serial analyzer (built once — run_analysis's prepare() is
        # idempotent), the DINOv2 Embedder (rebuilt only when the active model's
        # (backbone, imgsz) changes), and the loaded gallery (reloaded only when the
        # active model's gallery_path changes). Never read by status(), so no lock.
        self._analyzer: "object | None" = None
        self._resident: "object | None" = None
        self._resident_gallery: "object | None" = None
        self._resident_gallery_path: "str | None" = None

        # The frame watermark: the max span end already processed, so a tick only asks
        # closed_visits for spans beyond it. Seeded from the persisted setting so a
        # restart resumes where it left off (default 0 = scan from the start; the
        # idempotent resume queries make a stale value merely re-work, never a mislabel).
        raw = store.get_setting(_WATERMARK_KEY)
        self._watermark = int(raw) if raw is not None else 0

        # True until the first start() when NO watermark has ever been persisted — the
        # very first enable. start() then jumps the watermark to the current frame
        # horizon so the worker names only NEW visits, never back-identifying the whole
        # existing store (that is the manual Identify pass's job). A persisted watermark
        # (a restart / launch-time restore) is never absent, so this stays False there
        # and the worker resumes where it left off. See start().
        self._seed_horizon = raw is None

        # Observability for /api/stats: when the last tick ran, and the most-recent tick
        # error (sticky until the next error, so a returning operator still sees it).
        self._last_tick_ts: "int | None" = None
        self._last_error: "str | None" = None

    # --- The tick ------------------------------------------------------------------------

    def _ensure_resident(self, model: dict) -> None:
        """(Re)build the resident ``Embedder`` + gallery iff absent or the model changed.

        Keeps the worker's query embedder in the SAME feature space as the active
        gallery: rebuild only when ``(backbone, imgsz)`` differs from the resident one (a
        fresh promotion of a differently-configured model), otherwise reuse it across
        ticks so a visit never triggers a ``torch.hub`` weight reload. ``run_identify``
        re-checks this match and raises on a mismatch, so keeping them in sync here is
        what keeps that guard silent. The gallery ``.npz`` is likewise loaded once and
        reused, reloaded only when ``gallery_path`` changes (a promotion) — so a visit
        also never re-reads it off disk. Worker-thread only; no lock.
        """
        if (
            self._resident is None
            or self._resident.backbone != model["backbone"]
            or self._resident.imgsz != model["imgsz"]
        ):
            embedder = self._embedder_factory(model=model["backbone"], imgsz=model["imgsz"])
            embedder.prepare()
            self._resident = embedder
        if self._resident_gallery is None or self._resident_gallery_path != model["gallery_path"]:
            self._resident_gallery = self._gallery_loader(model["gallery_path"])
            self._resident_gallery_path = model["gallery_path"]

    def _tick(self, stop_event: threading.Event) -> None:
        """Run ONE identification pass over the newly-closed visits on the settled tail.

        Steps (see the live-identify-worker spec):

        1. ``store.active_model()`` — idle this tick if ``None`` (nothing to identify
           against; a promotion is picked up here with no operator action).
        2. If ``is_busy()`` a manual analysis/training job holds the GPU — skip, leaving
           the watermark untouched so the tick simply resumes next interval.
        3. Ensure the resident embedder matches the active model, and build the
           yolo-serial analyzer once (reused across ticks; ``run_analysis`` prepares it
           idempotently).
        4. ``store.closed_visits(watermark, now)`` — the ``[lo, hi]`` spans of motion
           clusters whose last frame has settled (older than the visit gap), beyond the
           watermark. The SAME clustering ``events()``/``visits()`` use, so no drift.
        5. For each span, in order (at most ``_MAX_SPANS_PER_TICK`` per tick, the rest
           deferred to the next interval): re-check stop / ``is_busy`` and yield the GPU
           immediately if either fired; else detect (yolo-serial, scoped to the span —
           including the calm ``motion=0`` frames a cat paused on, which often identify
           best), then identify against the active gallery, then advance + persist the
           watermark to ``hi``. The watermark is advanced ONLY after BOTH passes complete
           for the span, so a stop that leaves detect partial (it returns normally between
           batches) or cancels identify (``EmbedCancelled`` at a batch boundary) parks the
           watermark BEFORE that span — the idempotent resume finishes it next run rather
           than leaving it permanently under-identified.

        The whole body runs under one ``try``: a span failure (or any tick-level fault)
        is logged, recorded into ``last_error``, and stops the tick WITHOUT advancing the
        watermark past the failed span — the worker thread stays alive and the next tick
        retries, exactly mirroring the collector's per-frame error survival. A cancel is
        NOT a fault (it is an intentional stop), so it winds the tick down without touching
        ``last_error``. All heavy work is outside the lock; only the small status writes
        take it.
        """
        now = self._now_ms()
        with self._lock:
            self._last_tick_ts = now

        try:
            model = self._store.active_model()
            if model is None:
                return  # no promoted gallery — nothing to identify against this tick
            if self._is_busy():
                return  # a manual analysis/training job owns the GPU — yield, watermark untouched

            self._ensure_resident(model)
            if self._analyzer is None:
                # Built once and reused every span/tick; run_analysis.prepare() is idempotent.
                self._analyzer = self._analyzer_factory()

            detect_manager = _DetectAdapter(stop_event)
            for i, (lo, hi) in enumerate(self._store.closed_visits(self._watermark, now)):
                if i >= _MAX_SPANS_PER_TICK:
                    # Cap the tick: leave the rest of a backlog to the next interval so a
                    # single tick can't monopolize the GPU. The watermark is parked at the
                    # last processed span, so the next tick resumes right after it.
                    break
                if stop_event.is_set() or self._is_busy():
                    # Re-check per span, not just once at tick start: a stop OR a manual
                    # analysis/training job can arrive mid-tick, and the shared GPU means
                    # the worker must yield immediately rather than contend for the rest of
                    # a long tick. Watermark stays at the last fully-processed span.
                    break
                self._detect(self._store, self._analyzer, detect_manager, since_id=lo, until_id=hi)
                if stop_event.is_set():
                    # detect returns NORMALLY (not raising) when a stop aborts it between
                    # batches, leaving [lo, hi] only partially detected. Do NOT identify or
                    # advance past a partial detect — bail with the watermark before this
                    # span so the next run re-detects it whole.
                    break
                try:
                    self._identify(
                        self._store,
                        model,
                        model["gallery_path"],
                        since_id=lo,
                        until_id=hi,
                        embedder=self._resident,
                        gallery=self._resident_gallery,
                        progress=lambda done, total: not stop_event.is_set(),
                    )
                except EmbedCancelled:
                    # A stop interrupted identify at a batch boundary: the span is only
                    # partly identified, so leave the watermark before it (the idempotent
                    # resume finishes it next run) and wind the tick down. A cancel is
                    # intentional, not a fault — do not record it as last_error.
                    break
                # Advance ONLY after both passes for this span succeeded — so a failure
                # above never lets the watermark skip an un-identified span.
                with self._lock:
                    self._watermark = hi
                self._store.set_setting(_WATERMARK_KEY, str(hi))
        except Exception as exc:
            # A per-span (or setup) fault must not kill the always-on worker: log it,
            # surface it on status().last_error, and stop the tick here — the watermark is
            # already parked at the last good span, so the next tick simply retries.
            logger.exception("live-identify: tick failed")
            with self._lock:
                self._last_error = str(exc)

    # --- Lifecycle (mirrors CollectorManager) --------------------------------------------

    def start(self) -> None:
        """Start the worker; idempotent, and persists the on intent.

        A stopped run is replaced by a FRESH thread + stop event (both one-shot). Before
        spawning the replacement we best-effort join any prior thread (bounded by
        ``_STOP_JOIN_TIMEOUT_S``) so a rapid stop→start doesn't leave the previous worker
        briefly overlapping on the shared GPU; if the join times out we proceed anyway
        (the old thread carries the old, set stop event and exits at its next boundary).
        The intent is persisted so a launch can restore it (see ``restore``).

        On the very first enable (no watermark ever persisted) the watermark is seeded to
        the current frame horizon, so the worker names only NEW visits and does not
        back-identify the whole existing store — see ``_seed_horizon``.
        """
        # Seed BEFORE (and outside) the lock — it reads/writes the store, which must never
        # happen under the manager lock. Guarded to fire once, so a start() while already
        # running (below) is unaffected.
        if self._seed_horizon:
            horizon = self._store.latest_id()
            self._store.set_setting(_WATERMARK_KEY, str(horizon))
            with self._lock:
                self._watermark = horizon
            self._seed_horizon = False
        with self._lock:
            if self._running:
                return
            stale = self._thread
            stop_event = threading.Event()
            thread = threading.Thread(
                target=run_live_identify,
                args=(self, stop_event, self._tick_seconds),
                name="live-identify",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            self._running = True
        # Join the previous thread and start the new one OUTSIDE the lock (a status poll
        # must not stall behind the join); persist intent via the store's own lock, never
        # nested inside the manager lock.
        if stale is not None and stale.is_alive():
            stale.join(timeout=_STOP_JOIN_TIMEOUT_S)
        thread.start()
        self._store.set_setting(_INTENT_KEY, "1")

    def stop(self) -> None:
        """Stop the worker; idempotent. Signals the loop, flips ``running``, persists off.

        Sets the current stop event and clears ``running`` synchronously so the next
        ``/api/stats`` poll sees "stopped" at once. It does NOT join — the thread may be
        parked in ``stop_event.wait`` or mid-detect and would only notice at the next
        boundary; the daemon winds down on its own, and ``start`` handles any leftover
        before spawning a replacement. ``join`` (below) is the shutdown-only wait.
        """
        with self._lock:
            if not self._running:
                return
            if self._stop_event is not None:
                self._stop_event.set()
            self._running = False
        self._store.set_setting(_INTENT_KEY, "0")

    def restore(self, flag: bool) -> None:
        """Start the worker iff the persisted intent was on — the launch-time restore.

        Mirrors ``CollectorManager.restore_motion_only``: called by ``create_app`` on a
        live app with ``store.get_setting("live_identify") == "1"``. A falsy flag is a
        no-op (stay stopped); a truthy flag goes through ``start`` (which itself persists
        "1" again — harmless, the intent is already "1").
        """
        if flag:
            self.start()

    def join(self, timeout: "float | None" = None) -> None:
        """Best-effort wait for the worker thread to exit — for shutdown only.

        ``stop()`` deliberately doesn't join (it must not stall an HTTP handler). At
        process exit, though, the store's connection is about to close and the worker
        writes through it (``set_setting``, and the detect/identify passes), so pair
        ``stop()`` with this ``join`` so an in-flight tick finishes before
        ``store.close()`` rather than racing a closed DB. The thread parks in
        ``stop_event.wait`` and notices the stop at the next boundary, so pass a
        ``timeout`` to bound how long exit waits; it is a daemon and dies with the
        process regardless. Snapshot the reference under the lock, join OUTSIDE it.
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def running(self) -> bool:
        """Whether the worker is currently on (lock-guarded read of the intent flag)."""
        with self._lock:
            return self._running

    def status(self) -> dict:
        """A consistent snapshot for the ``/api/stats`` poll (lock-guarded).

        ``running`` is the intent flag; ``watermark`` is the max span end processed;
        ``last_tick_ts`` is when the last tick ran (``None`` before the first); and
        ``last_error`` is the most-recent tick error (``None`` if none yet, sticky until
        the next error so a returning operator still sees it).
        """
        with self._lock:
            return {
                "running": self._running,
                "watermark": self._watermark,
                "last_tick_ts": self._last_tick_ts,
                "last_error": self._last_error,
            }

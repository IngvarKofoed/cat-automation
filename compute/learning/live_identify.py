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
from compute.analysis.runner import DetectAdapter, run_analysis
from compute.identification.embed import (
    EmbedCancelled,
    Embedder,
    canonical_geometry,
    parse_geometry,
)
from compute.identification.gallery import (
    _geometry_kwargs,
    _model_geometry,
    load_gallery,
    run_identify,
)
from compute.learning.deterrent import (
    ALARM_TICK_S,
    DETER_MAX_FRAMES,
    DETER_WINDOW_S,
    ENDED_ERROR,
    ENDED_INTERRUPTED,
    MODE_ON,
    Alarm,
    get_mode,
    is_calibrated,
)

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


def _idle_pass(caught_up: bool, behind: int) -> dict:
    """The alarm pass summary when nothing was processed — the same shape as a real pass."""
    return {"ids": 0, "ms": 0, "detect_ms": 0, "identify_ms": 0, "caught_up": caught_up, "behind": behind}


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
    elapsed = 0.0
    while not stop_event.is_set():
        # While the foreign-cat alarm switch is On the period is a FIXED 1 s — the wait is
        # what is left of it after the tick — so the alarm sees new frames, and the "gone"
        # check runs, about once a second. Off, the plain interval as before.
        if stop_event.wait(manager._next_wait(elapsed, tick_seconds)):
            break
        started = time.monotonic()
        try:
            manager._tick(stop_event)
        except Exception:  # pragma: no cover - _tick already contains its own guard
            # _tick catches its own errors into last_error; this is a belt-and-braces
            # net so no path can leave the always-on worker dead with running=True.
            logger.exception("live-identify: tick crashed unexpectedly")
        elapsed = time.monotonic() - started
    # An alarm episode lives in this worker's memory: once the thread is gone nothing can
    # stop it or complete its row, so it ends here, on the way out, as `interrupted` — the
    # word a restart's launch-time sweep uses for the same situation.
    manager._alarm_end_safely(ENDED_INTERRUPTED, manager._now_ms())
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
        sound=None,
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

        # Bumped by every ``reset_watermark``. A tick snapshots it and refuses to advance the
        # watermark once it has changed, because ``/api/clear`` resets mid-flight: the tick
        # queried spans from the PRE-wipe watermark and would otherwise write its own derived
        # value back, resurrecting a watermark above every post-wipe frame — naming would then
        # stop silently. See ``_advance_watermark``.
        self._epoch = 0

        # Observability for /api/stats: when the last tick ran, and the most-recent tick
        # error (sticky until the next error, so a returning operator still sees it).
        self._last_tick_ts: "int | None" = None
        self._last_error: "str | None" = None

        # The foreign-cat alarm (compute/learning/deterrent.py). ``sound`` is the Pi
        # client the clips are requested through (an ``EdgeClient``; None = no Pi, so
        # every clip request fails and is counted). The episode rules live in ``Alarm``;
        # this worker owns the GPU half — detecting + identifying the newest frames.
        self._alarm = Alarm(store, sound)
        # The alarm step's high-water mark: the highest frame id it has processed. In
        # memory only (a restart starts at the next open visit), guarded by ``_lock`` and
        # re-seeded by ``reset_watermark`` exactly as the watermark is.
        self._alarm_hwm = 0
        # The previous pass's outcome, read by the next tick's bookkeeping: whether it
        # raised (an alarm must not keep sounding on a step that can't see), and whether it
        # caught up with the store (the "gone" check is meaningless otherwise). Worker-only.
        self._alarm_pass_failed = False
        self._alarm_caught_up = False

    # --- The tick ------------------------------------------------------------------------

    def _ensure_resident(self, model: dict) -> None:
        """(Re)build the resident ``Embedder`` + gallery iff absent or the model changed.

        Keeps the worker's query embedder in the SAME feature space as the active
        gallery: rebuild only when ``(backbone, imgsz, geometry)`` differs from the
        resident one (a fresh promotion of a differently-configured model), otherwise
        reuse it across ticks so a visit never triggers a ``torch.hub`` weight reload.
        ``run_identify`` re-checks this match and raises on a mismatch, so keeping them in
        sync here is what keeps that guard silent. The gallery ``.npz`` is likewise loaded
        once and reused, reloaded only when ``gallery_path`` changes (a promotion) — so a
        visit also never re-reads it off disk. Worker-thread only; no lock.

        Geometry rides ``_geometry_kwargs``, which yields ``{}`` for a legacy model — so
        the factory call stays byte-identical for every gallery built before geometry
        existed, and a duck-typed test factory that predates the parameter keeps working.
        Without this the guard would fire on the first non-legacy promotion and park a
        sticky ``last_error`` on the worker instead of naming cats.
        """
        want_geometry = _model_geometry(model)
        if (
            self._resident is None
            or self._resident.backbone != model["backbone"]
            or self._resident.imgsz != model["imgsz"]
            # Canonicalised on BOTH sides, exactly as `run_identify`'s guard does — this
            # is the same check written twice, and a real `Embedder` returns an already
            # canonical descriptor, so the difference only shows through the duck-typed
            # factory seam. There an un-normalised stamp would never compare equal, and
            # the miss is silent: the embedder is rebuilt EVERY tick (a torch.hub reload
            # per visit, which this method exists to prevent) before run_identify raises.
            or canonical_geometry(getattr(self._resident, "geometry", None)) != want_geometry
        ):
            letterbox, margin = parse_geometry(want_geometry)
            embedder = self._embedder_factory(
                model=model["backbone"],
                imgsz=model["imgsz"],
                **_geometry_kwargs(letterbox, margin),
            )
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

        # The foreign-cat alarm step runs FIRST, before any early return below: its
        # bookkeeping must run every tick (an alarm stops when the switch goes Off even
        # with no model promoted), and it never yields to manual jobs. ``gate`` is the
        # switch being On with a calibrated gallery; then the closed-visit pass below
        # waits while a visit is open OR an alarm is sounding, because naming one closed
        # span can take seconds and would stall the trigger, the next clip and the "gone"
        # check behind housekeeping that is in no hurry. (A sounding alarm with no open
        # visit is a stranger sitting still in Tuning — the spec's rule named only the
        # open visit.)
        gate, open_span = self._alarm_step(stop_event, now)
        if gate and (open_span is not None or self._alarm.running):
            return

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

            detect_manager = DetectAdapter(stop_event)
            # Snapshot the watermark AND the epoch together under the lock, so this tick's span
            # query and its staleness check agree; store reads stay outside the lock.
            with self._lock:
                epoch, watermark = self._epoch, self._watermark
            latest = self._store.latest_id()
            if watermark > latest:
                # Frame ids REGRESSED below the watermark. ``frames.id`` is INTEGER PRIMARY KEY
                # with NO AUTOINCREMENT, so SQLite REUSES rowids once the max row is deleted —
                # and the non-motion purge deletes through the current max id, which at ~5 fps
                # is almost always a non-motion frame. Left alone, ``closed_visits(watermark)``
                # returns nothing on EVERY later tick, so naming silently stops (a
                # user-VISIBLE outage: the Activity feed stops naming cats) until the store
                # regrows past the stale id. Clamp so naming self-heals next tick.
                logger.warning(
                    "live-identify: frame ids regressed (watermark %d > latest %d) — clamping",
                    watermark,
                    latest,
                )
                if not self._advance_watermark(latest, epoch):
                    return
                watermark = latest
            for i, (lo, hi) in enumerate(self._store.closed_visits(watermark, now)):
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
                if gate and i > 0:
                    # Likewise a visit can OPEN mid-pass: stop and give the next tick's
                    # alarm step the GPU. (Span 0 was checked by the alarm step itself.)
                    with self._lock:
                        current = self._watermark
                    if self._store.open_visit(current, self._now_ms()) is not None:
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
                # above never lets the watermark skip an un-identified span. A False return
                # means a reset landed mid-tick, so this window is stale — abandon it rather
                # than write a pre-wipe-derived watermark.
                if not self._advance_watermark(hi, epoch):
                    break
        except Exception as exc:
            # A per-span (or setup) fault must not kill the always-on worker: log it,
            # surface it on status().last_error, and stop the tick here — the watermark is
            # already parked at the last good span, so the next tick simply retries.
            logger.exception("live-identify: tick failed")
            with self._lock:
                self._last_error = str(exc)

    # --- The foreign-cat alarm step (docs/specs/2026-11-01-foreign-cat-alarm.md) -------

    def _next_wait(self, elapsed: float, tick_seconds: float) -> float:
        """Seconds the loop waits before the next tick.

        Switch On: what is left of a fixed ``ALARM_TICK_S`` period after a tick that took
        ``elapsed`` (never more than the plain interval, so a short test interval stays
        short). Off — or the setting unreadable — the plain interval, as before the alarm.
        """
        try:
            on = get_mode(self._store) == MODE_ON
        except Exception:  # noqa: BLE001 - a store fault must not kill the loop
            on = False
        if not on:
            return tick_seconds
        return min(tick_seconds, max(0.0, ALARM_TICK_S - elapsed))

    def _alarm_end_safely(self, reason: str, now: int) -> None:
        try:
            self._alarm.end(reason, now)
        except Exception:  # noqa: BLE001 - the episode is already cleared in memory
            logger.exception("live-identify: could not record the alarm's end")

    def _alarm_step(self, stop_event: threading.Event, now: int) -> "tuple[bool, tuple[int, int] | None]":
        """Steps 1-3 of a tick: episode bookkeeping, process the newest frames, maybe start.

        Returns ``(gate, open_span)``. ``gate`` is the switch On with a calibrated gallery
        — the only state in which frames are processed, an alarm can start, and the
        closed-visit pass waits for an open visit; ``open_span`` is that open visit, if
        any. Never raises: a fault in the PASS is recorded so the next tick's bookkeeping
        ends a running episode as ``error``, and naming carries on below regardless.

        The pass deliberately ignores ``is_busy``. Manual jobs include the ~44-min
        motion-tuning re-runs, "Identify all" and the phone's "Analyse this visit", all of
        which run unattended — yielding would silently disarm the door for hours. The pass
        is bounded at ``DETER_MAX_FRAMES`` instead, as the always-on detection worker,
        which also never yields to them, already is.
        """
        try:
            mode_on = get_mode(self._store) == MODE_ON
            model = self._store.active_model()
        except Exception as exc:  # noqa: BLE001
            logger.exception("live-identify: alarm step could not read its state")
            self._alarm_end_safely(ENDED_ERROR, now)
            self._alarm.record_pass(None, str(exc))
            return False, None
        gate = mode_on and is_calibrated(model)

        # 1. Episode bookkeeping: end it, or play the next clip once the last is due to end.
        try:
            self._alarm.bookkeeping(
                mode_on=mode_on,
                model=model,
                pass_failed=self._alarm_pass_failed,
                caught_up=self._alarm_caught_up,
                now_ms=now,
            )
        except Exception as exc:  # noqa: BLE001 - its reads/writes are store calls
            logger.exception("live-identify: alarm bookkeeping failed")
            self._alarm_end_safely(ENDED_ERROR, now)
            with self._lock:
                self._last_error = str(exc)
        self._alarm_pass_failed = False
        if not gate:
            self._alarm_caught_up = False
            return False, None

        # 2. Process the newest frames.
        open_span = None
        try:
            open_span, info = self._alarm_process(stop_event, model, now)
        except EmbedCancelled:
            self._alarm_caught_up = False  # a stop — not a fault
            return True, open_span
        except Exception as exc:  # noqa: BLE001
            logger.exception("live-identify: alarm pass failed")
            self._alarm_pass_failed = True
            self._alarm_caught_up = False
            self._alarm.record_pass(None, str(exc))
            with self._lock:
                self._last_error = str(exc)
            return True, open_span
        self._alarm_caught_up = info["caught_up"]
        self._alarm.record_pass(info, None)
        if stop_event.is_set():
            return True, open_span

        # 3. Start, if no alarm is running and the recent counted frames vote foreign.
        if not self._alarm.running:
            try:
                visit_start_ts = (
                    self._store.frame_recv_ts(open_span[0]) if open_span is not None else None
                )
                # Behind, the frontier is the last frame this pass processed: every frame
                # still to come lies after it, so it bounds what an absence may say.
                frontier_ms = None
                if not info["caught_up"]:
                    with self._lock:
                        hwm = self._alarm_hwm
                    frontier_ms = self._store.frame_recv_ts(hwm)
                self._alarm.maybe_start(
                    model, self._now_ms(), visit_start_ts,
                    caught_up=info["caught_up"], frontier_ms=frontier_ms,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("live-identify: alarm start failed")
                self._alarm_pass_failed = True
                self._alarm.record_pass(None, str(exc))
                with self._lock:
                    self._last_error = str(exc)
        return True, open_span

    def _alarm_process(
        self, stop_event: threading.Event, model: dict, now: int
    ) -> "tuple[tuple[int, int] | None, dict]":
        """Detect + identify the frames above the high-water mark, at most ``DETER_MAX_FRAMES``.

        With no alarm running, only the OPEN visit's frames: the mark jumps to just below
        the visit's first frame (the frames between visits are empty scene) or the start
        window's, whichever is later, and with no open visit nothing is processed. While an alarm runs, every stored frame above the
        mark, in a visit or not — in Tuning that keeps a stranger sitting still in view.
        The mark is clamped to the newest frame (ids regress after a Clear or a purge, and
        a mark left above every frame would silently process nothing) and only advances
        when no ``reset_watermark`` landed meanwhile.

        Processing is the closed pass's own ``_detect`` then ``_identify``, with the same
        idempotency, so that pass later skips everything done here. Returns the open
        visit and a pass summary whose ``caught_up`` says no frame was left above the mark.
        """
        with self._lock:
            epoch, watermark, hwm = self._epoch, self._watermark, self._alarm_hwm
        running = self._alarm.running
        open_span = self._store.open_visit(watermark, now)
        latest = self._store.latest_id()
        hwm = min(hwm, latest)
        if not running:
            if open_span is None:
                self._set_alarm_hwm(hwm, epoch)
                return None, _idle_pass(True, 0)
            start_id, latest = open_span
            # Jump to the visit's first frame — but no further back than DETER_WINDOW_S
            # before now: replaying a long visit oldest-first (switch turned On, or the
            # worker restarted, mid-visit) would only delay the start by minutes. The
            # skipped frames are the closed pass's to name, as every other closed visit's
            # are. Known limit, accepted (alarm-presence-vote spec): the presence vote
            # reaches back further than this, so frames skipped here — after a switch-on or
            # restart mid-presence, or whenever the step lags the camera by more than
            # DETER_WINDOW_S — are absent from that vote.
            window_first, _ = self._store.resolve_ts_range(now - int(DETER_WINDOW_S * 1000), None)
            floor = start_id - 1 if window_first is None else max(start_id, window_first) - 1
            hwm = max(hwm, floor)
        lo = hwm + 1
        if lo > latest:
            self._set_alarm_hwm(hwm, epoch)
            return open_span, _idle_pass(True, 0)
        hi = min(latest, hwm + DETER_MAX_FRAMES)

        # The pass is timed whole (`ms`) and per half, so where its ~1 s fixed cost goes —
        # detection or identification — is readable from /api/deterrent. Each half is
        # truncated to whole ms off the same clock, so the two never sum past `ms`.
        started = time.monotonic()
        self._ensure_resident(model)
        if self._analyzer is None:
            self._analyzer = self._analyzer_factory()
        detect_started = time.monotonic()
        self._detect(self._store, self._analyzer, DetectAdapter(stop_event), since_id=lo, until_id=hi)
        detect_ended = time.monotonic()
        if stop_event.is_set():
            return open_span, _idle_pass(False, latest - hwm)
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
        ended = time.monotonic()
        self._set_alarm_hwm(hi, epoch)
        return open_span, {
            "ids": hi - lo + 1,
            "ms": int((ended - started) * 1000),
            "detect_ms": int((detect_ended - detect_started) * 1000),
            "identify_ms": int((ended - detect_ended) * 1000),
            "caught_up": hi >= latest,
            "behind": latest - hi,
        }

    def _set_alarm_hwm(self, value: int, epoch: int) -> None:
        with self._lock:
            if epoch == self._epoch:
                self._alarm_hwm = int(value)

    def alarm_status(self) -> dict:
        """The alarm's snapshot for ``GET /api/deterrent`` (episode, last pass, its error)."""
        status = self._alarm.status()
        with self._lock:
            status["hwm"] = self._alarm_hwm
        return status

    def _advance_watermark(self, value: int, epoch: int) -> bool:
        """Set + persist the watermark, unless a ``reset_watermark`` landed since ``epoch``.

        Returns False when the epoch has moved — i.e. ``/api/clear`` re-seeded the watermark
        while this tick was in flight, so the tick's spans were queried against the PRE-wipe
        store. Writing a value derived from it would resurrect a watermark above every
        post-wipe frame and silently stop naming (the exact failure ``reset_watermark`` exists
        to prevent), so the caller must abandon the tick instead. In-memory first, then
        persisted: a crash between the two costs re-work, never a missed visit.
        """
        with self._lock:
            if epoch != self._epoch:
                return False
            self._watermark = int(value)
        self._store.set_setting(_WATERMARK_KEY, str(int(value)))
        return True

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

    def stop(self, persist: bool = True) -> None:
        """Stop the worker; idempotent. Signals the loop and flips ``running``.

        Sets the current stop event and clears ``running`` synchronously so the next
        ``/api/stats`` poll sees "stopped" at once. It does NOT join — the thread may be
        parked in ``stop_event.wait`` or mid-detect and would only notice at the next
        boundary; the daemon winds down on its own, and ``start`` handles any leftover
        before spawning a replacement. ``join`` (below) is the shutdown-only wait.

        ``persist`` distinguishes an OPERATOR stop from a PROCESS stop: the shutdown hook
        also calls ``stop()``, so persisting "0" unconditionally would clear the operator's
        on-intent on every clean exit — leaving ``restore`` to fire only via its
        active-model clause, never via the intent it is meant to honor. The route passes the
        default (a deliberate off, remembered); ``_shutdown`` passes ``persist=False`` (a
        process exit, intent preserved). Same split the collector makes by persisting its
        intent in the route rather than the manager.
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

        Mirrors ``CollectorManager.restore_motion_only``: called by ``create_app`` on a
        live app with ``store.get_setting("live_identify") == "1"``. A falsy flag is a
        no-op (stay stopped); a truthy flag goes through ``start`` (which itself persists
        "1" again — harmless, the intent is already "1").
        """
        if flag:
            self.start()

    def reset_watermark(self, value: int) -> None:
        """Re-point the watermark (and its persisted copy) at ``value`` — for ``/api/clear``.

        ``Store.clear()`` wipes the frames but deliberately KEEPS the settings KV, while
        frame rowids restart from 1. A watermark left at the pre-wipe horizon would then sit
        far AHEAD of every new visit, so ``closed_visits(watermark, …)`` would never return
        one and the worker would look enabled while silently naming nothing — until the store
        re-grew past the old id. The idempotent resume queries cannot save us here: they are
        only consulted for spans the watermark already admitted.

        Two subtleties, both load-bearing (see ``YoloOracleManager.reset_watermark``, which is
        identical):

        - It bumps ``_epoch``, so a tick already in flight (``/api/clear`` does NOT stop the
          workers) refuses to write its pre-wipe-derived watermark back over this reset.
        - It is a NO-OP on a worker that has never been enabled: such a worker has no stale
          watermark to strand (it is 0, and the first ``start()`` seeds it to the THEN-current
          horizon), so writing the key here would only consume ``_seed_horizon`` — which is
          re-derived at construction as "no persisted key", so it would stay consumed across
          restarts. The cost of getting that wrong is a first enable back-IDENTIFYING the
          whole store, which changelog 75 reserves for the manual Identify pass.
        """
        with self._lock:
            # Bump first and unconditionally: any in-flight tick must be invalidated even in
            # the never-enabled case (cheap, and it keeps the invariant simple).
            self._epoch += 1
            # The alarm step's high-water mark is re-seeded with it, for the same reason:
            # left above every post-wipe frame, the step would process nothing, silently.
            self._alarm_hwm = int(value)
            if self._seed_horizon:
                return
            self._watermark = int(value)
        self._store.set_setting(_WATERMARK_KEY, str(int(value)))

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

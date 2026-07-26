# Continuous full-coverage YOLO oracle worker

An always-on background worker on the compute PC that runs `yolo-serial` over the
**full-coverage** frame tail (motion *and* non-motion) as frames arrive, so the
gate-scorecard oracle verdicts are pre-computed and opening a day on Motion tuning
no longer means waiting on a manual sweep. It is a dedicated worker
(`YoloOracleManager`) modelled almost exactly on `LiveIdentifyManager`, exposed as
a "YOLO all" toggle on the admin-next Start page's Capture-mode card, meaningful
only while capture is in keep-all mode.

## Key decisions

- **New dedicated worker** (new). `compute/learning/yolo_oracle.py` —
  `run_yolo_oracle` (daemon loop) + `YoloOracleManager` (lifecycle), a near-clone
  of `compute/learning/live_identify.py`. Kept off the `AnalysisManager` FIFO for
  the reasons that file already states (an endless re-enqueue pollutes job history
  and contends on the operator's queue slot). Chosen over folding into
  `live_identify`: oracle-production (no gallery, covers non-motion) and naming
  (needs a gallery, motion-only) are genuinely different jobs.
- **Detect-only, `yolo-serial`, via `run_analysis`** (reuses). The tick calls
  `run_analysis(store, get_analyzer("yolo-serial"), _DetectAdapter(stop_event),
  since_id=…, until_id=…)` — the exact code path the manual sweep uses, so verdicts
  are byte-identical (verdict parity is why the oracle must be serial, per the
  `yolo-batch-vs-serial-diverges` finding). No identify step and **no
  `active_model()` dependency** — the worker runs regardless of whether a gallery
  is promoted.
- **Watermark tail, seeded to horizon on first enable** (reuses). A persisted
  `yolo_oracle_watermark` bounds the tick to new frames; `iter_unanalyzed` is the
  idempotent correctness backstop only *within* the tick window, so the watermark
  must never sit ahead of the frames it's meant to cover. First-ever enable jumps
  the watermark to `store.latest_id()` so the worker covers only frames *going
  forward* — historical days stay the manual sweep's job — and `/api/clear`
  re-seeds it to the post-wipe horizon (see Wiring), since `clear()` keeps the
  settings KV while rowids reset, which would otherwise strand the worker behind a
  stale-high watermark.
- **Per-tick frame cap, processed in chunks** (extends). A tick processes at most
  `_MAX_FRAMES_PER_TICK` frames, in `_CHUNK`-sized sub-windows, re-checking
  `stop_event`/`is_busy` between chunks — the frame-count analogue of
  live_identify's per-span loop. The cap bounds a resume-after-gap drain; the
  between-chunk re-check is what makes a manual job enqueued mid-tick win promptly
  (`run_analysis` itself only honours `stop_event`, not `is_busy`, so a single
  monolithic window would hold the GPU for the whole ≤2000 frames).
- **Idles when capture is motion-only** (new). The tick takes an injected
  `motion_only` getter (like `run_collector` does) and no-ops when it returns True
  — enforcing "full coverage needs non-motion frames" in the backend, not just by
  greying the toggle. Intent stays on; work simply resumes when capture returns to
  keep-all.
- **Independent workers; both yield only to manual jobs** (reuses). The oracle's
  `is_busy` is `analysis_manager.running or training_manager.running` — the exact
  predicate `live_identify` already uses, and `live_identify` is left **unchanged**.
  The two always-on YOLO loops do **not** yield to each other. This is safe because
  a same-frame detect is idempotent: `analysis` is `PRIMARY KEY (frame_id,
  analyzer)` written via `INSERT OR REPLACE`, and all writes serialize on the store
  lock — so an occasional overlap (both detecting a motion frame) just wastes a
  little GPU work, never corrupts. Rejected an inter-worker "one thread at a time"
  yield: a cloned `.running` is an *intent* flag (true whenever the toggle is on),
  so feeding it into `live_identify`'s `is_busy` would suppress naming for the whole
  time the oracle is enabled. Cost accepted: two `yolo-serial` models resident in
  GPU memory (small) and rare duplicate detects.
- **Persisted intent, restore gated on `start_collector`, no active-model
  auto-start** (diverges). Restores only from the persisted `yolo_oracle` intent
  (operator left it on), gated on `start_collector` like `live_identify.restore`.
  It deliberately does **not** auto-start when a model is promoted — that is a
  run-mode naming concern; the oracle is a tuning tool.
- **Sixth shared-connection writer in shutdown** (extends). Added to the
  `_shutdown` stop-then-join sequence before `store.close()`, same load-bearing
  ordering as the existing five writers.
- **Payoff surfaces on existing UI** (reuses). The Motion-tuning "YOLO coverage"
  readout already shows frames-swept vs total; with the oracle on it trends to full
  on its own. No new coverage view — only the Start-page toggle plus a small
  running/last-tick status from `status()`, folded into `/api/stats` like
  `live_identify`.

## Goals

- Eliminate the per-day manual `yolo-serial` sweep wait when tuning the motion
  gate: coverage for any window captured while the worker was on (in keep-all mode)
  is already ~100%.
- Give the gate scorecard *uniform* full-coverage oracle verdicts over such
  windows — resolving the non-uniform-coverage caveat that makes a live-identify-
  populated window untunable (changelog #76).
- Reuse the manual sweep's exact detection path so live and pre-computed verdicts
  never diverge.

## Non-goals

- **Backfilling history.** First enable covers only new frames; older days still
  need a manual sweep. (Same principle as live-identify's horizon seed.)
- **Re-detecting after a detector change.** The worker only fills *missing*
  verdicts (`iter_unanalyzed`); a broadened detector (person/bird, #89–90) still
  needs a manual `reanalyze` sweep to replace stale rows.
- **Replacing live-naming or the manual sweep.** It supplements both.
- **Running in motion-only capture mode.** By construction it idles there.
- **Any actuation / decision-path change.** Pure offline tuning support.

## Design

### The worker (`compute/learning/yolo_oracle.py`)

Two pieces mirroring `live_identify.py`:

- `run_yolo_oracle(manager, stop_event, tick_seconds)` — `while not stop: if
  stop_event.wait(tick): break; manager._tick(stop_event)`, with the same
  belt-and-braces `except` guard so a tick fault can never leave the daemon dead
  with `running=True`.
- `YoloOracleManager` — the lifecycle: authoritative `running` intent flag (not
  `thread.is_alive()`), a fresh `(thread, stop_event)` per `start`, bounded
  best-effort join of a prior thread, persisted intent + watermark, `status()`
  snapshot, `restore(flag)`, and `join(timeout)` for shutdown.

Constructor injection seams (so the dev box can test with fakes, no torch):
`store`, `is_busy: Callable[[], bool]`, `motion_only: Callable[[], bool]`,
`detect=run_analysis`, `analyzer_factory=lambda: get_analyzer("yolo-serial")`,
`tick_seconds`, `now_ms`. Note it needs **no** identify/embedder/gallery seams —
that whole half of `LiveIdentifyManager` is absent.

`_tick(stop_event)`:

1. If `self._is_busy()` → return (yield the GPU; watermark untouched).
2. If `self._motion_only()` → return (full coverage impossible; idle).
3. Build the `yolo-serial` analyzer once, reused across ticks (`run_analysis`
   prepares it idempotently); reuse the `_DetectAdapter(stop_event)` stand-in
   (`stop_event` + no-op `set_total`/`record`). Since two workers now need it,
   **extract `_DetectAdapter` from `live_identify.py` into a shared spot** (e.g.
   `compute/analysis/runner.py`) and import it in both — not a second copy.
4. Window: `since = self._watermark + 1`; `until = min(store.latest_id(),
   self._watermark + _MAX_FRAMES_PER_TICK)` (`_MAX_FRAMES_PER_TICK = 2000`; only
   bites on a resume-after-gap drain). If `until < since` → nothing new, return.
5. Walk `[since, until]` in `_CHUNK`-sized (~256) sub-windows. Before each chunk
   re-check `stop_event`/`is_busy` and bail immediately if either fires. Each chunk
   is `self._detect(self._store, analyzer, adapter, since_id=lo, until_id=hi)` —
   full coverage (`motion_only` defaults False in `run_analysis`), so both motion
   and non-motion frames get a verdict; `iter_unanalyzed` inside skips any already
   done (e.g. a motion frame live-identify raced to first). The between-chunk
   re-check is what lets a mid-tick manual job win within ~one chunk rather than
   after the whole window.
6. Advance + persist the watermark to each chunk's `hi` **only after** that chunk's
   detect returns without a pending stop — so a `stop_event`/`is_busy` bail parks
   the watermark at the last *completed* chunk and the next run resumes there.

The whole body is under one `try`; a fault is logged into `last_error`, the tick
stops without advancing the watermark, and the daemon survives — mirroring
live_identify's per-tick error survival.

### Wiring (`compute/api/app.py`)

- Build `YoloOracleManager` after the analysis/training managers (needs their
  `.running` for `is_busy`), lazily imported like `LiveIdentifyManager`. Pass
  `is_busy=(lambda: analysis_manager.running or training_manager.running)` — the
  same predicate `live_identify` uses — and
  `motion_only=collector_manager.current_motion_only` (the property getter).
- `live_identify`'s `is_busy` is **left unchanged** (the two workers are
  independent; see the coordination Key decision).
- `restore`: on `start_collector`, `yolo_oracle_manager.restore(store.get_setting(
  "yolo_oracle") == "1")` — persisted-intent only, no active-model clause.
- `_shutdown`: add `stop()` + `join(timeout=10.0)` before `store.close()`, after
  live-identify and before/with cleanup (any position among the writers works; keep
  it before `store.close()`).
- `/api/stats`: add `"yolo_oracle": yolo_oracle_manager.status()`.
- Routes `POST /api/yolo-oracle/start` and `/stop` mirroring the live-identify
  routes, returning `{running}`.
- `/api/clear`: after `store.clear()`, re-seed the oracle watermark to the post-wipe
  horizon (`store.latest_id()`) so it isn't stranded behind a stale-high value —
  `clear()` keeps the settings KV but resets rowids. (`live_identify`'s watermark
  has the same latent hazard; reset it in the same handler while we're here.)

### Frontend (`admin-next` Start page, Capture-mode card)

Add a "YOLO all" toggle beneath the two-button capture segment. Enabled only when
`s.motion_only` is false (greyed with a note otherwise: "keep-all mode only — the
oracle needs the non-motion frames"). Drives `POST /api/yolo-oracle/start|stop`,
reflects `s.yolo_oracle.running` from the existing 3 s `/api/stats` poll, and shows
a quiet last-tick / last-error line like the live-naming control. When the toggle
is on but capture is motion-only, the status line reads "idle — motion-only
capture" so an on-but-not-working worker isn't mistaken for broken. Nothing else on
the page changes; the coverage payoff shows up on the Motion-tuning page's existing
YOLO-coverage readout.

## Alternatives considered

- **Fold into `live_identify` as a full-coverage mode.** One always-on YOLO thread
  does both detect-all and motion-visit naming. Rejected: couples a gallery-
  dependent, motion-scoped concern with a gallery-independent, full-coverage one —
  the tick branches, the single watermark means two things, and the keep-all-only
  oracle toggle entangles with the live-naming toggle.
- **Self-re-enqueuing job on `AnalysisManager`.** Rejected by the existing design
  (pollutes job history, contends on the operator's queue slot) and the "off the
  FIFO" constraint.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** One new worker file closely templated on
  `live_identify.py`, plus focused edits to `app.py` (wiring/routes/stats/shutdown)
  and the admin-next Start page — one coherent thread that builds on itself, with a
  parallel pytest file cloned from `test_live_identify.py`. Nothing splits into
  independent streams; the design risk is in the tick/yield semantics, which want
  the strong tier.

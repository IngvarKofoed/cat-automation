# Live identification worker

Make the Activity page name **new visits automatically**, instead of the current
manual Identify pass. A new always-on background worker on the compute PC — a
`LiveIdentifyManager` mirroring `CollectorManager` — watches the settled tail of
collected frames, and for each *closed motion cluster* (the same unit `events()`
already shows as an event) runs `yolo-serial` detection then `run_identify`
against the active gallery. It reads `active_model()` each tick so a promotion is
picked up live, holds its detector + embedder resident across ticks, and yields
the GPU whenever a manual analysis/training job is running. `events()` is
untouched — it already names a cluster from whatever `identifications` rows fall
in its span; the worker just keeps those rows populated as visits happen.

## Key decisions

- **Dedicated `LiveIdentifyManager`** (new). A new manager mirroring
  `CollectorManager` (`compute/collection/collector.py`): a `(thread, stop_event,
  running-intent)` triple under a lock, with `start()/stop()/join()/status()`, a
  background daemon tick loop, and a shutdown stop-then-join before
  `store.close()`. Chosen over overloading the `AnalysisManager`/`TrainingManager`
  FIFOs, which are one-shot walk-away job lists, not an always-on loop.
- **Visit-scoped, settled tail** (new). The worker only touches *closed* motion
  clusters — a cluster whose last motion frame is older than `_VISIT_GAP_MS`, so
  no later frame can extend it. Both detection *and* identify are scoped to each
  cluster's `[start_id, end_id]`, so **the live worker spends GPU only inside
  visits** and never runs YOLO over the long non-motion stretches the collector
  also stores. This deliberately diverges from the manual `yolo-serial` sweep,
  which covers the whole store for gate validation — a different purpose.
- **Resident models + one refactor** (extends). The worker holds a prepared
  `yolo-serial` analyzer and a DINOv2 `Embedder` across ticks, rebuilding the
  embedder only when the active model's `(backbone, imgsz)` changes. This requires
  extending `run_identify` (`compute/identification/gallery.py`) to accept an
  optional pre-built `embedder`; today it constructs and `prepare()`s a new one
  every call (`torch.hub.load`), which per-cluster would reload weights every
  tick. The manual pass keeps passing `None` (unchanged).
- **Yields to manual jobs** (new). Before any GPU work the tick checks
  `analysis_manager.running()` / `training_manager.running()` and skips if either
  is busy. The GPU and the single SQLite connection are shared, so serial is the
  only safe execution; the watermark is untouched so the tick simply resumes next
  interval. Operator-initiated work always wins.
- **Promotion picked up live** (reuses `active_model()`). The tick re-reads
  `store.active_model()`, so a freshly promoted gallery becomes the target with no
  operator action — retiring the post-promote "re-run Identify" note *for new
  visits* (historical re-identification stays a manual backfill; see Design).
- **Injectable detect/identify** (reuses). The manager takes its detect and
  identify callables as constructor args, exactly as `TrainingManager` takes
  `identifier=run_identify`, so the whole tick/threading/lifecycle is testable
  with fakes on the GPU-less dev box — no torch.
- **Persisted intent** (reuses settings KV). The `live_identify` on/off intent is
  stored in the settings table and restored at launch (like the collector's
  motion-only intent, changelog 31/45), because the compute PC is the dedicated
  always-on box. A frame watermark is persisted too, but only as an optimization —
  the idempotent resume queries (`iter_unidentified`, `iter_unanalyzed`) are the
  correctness backstop.

## Goals

- New visits appear **named** in Activity with no manual Identify click.
- A promoted model reflects in going-forward naming automatically.
- Near-zero GPU cost outside visits; the collector is never blocked or starved.
- The worker's logic is fully unit-testable without a GPU.

## Non-goals

- **Historical re-identification** after promoting a new model — the manual
  Identify pass still owns re-naming earlier events against a new gallery.
- **Naming gate-missed visits** — if the edge motion gate saw no motion, there is
  no cluster and thus no Activity event to name. That is a gate-tuning concern.
- **Instant naming** — near-live (`~_VISIT_GAP_MS` + one tick) is the target, not
  sub-second.
- **Any actuation or decision** — this is Activity naming only.
- **Changing the identity aggregation or threshold** in `events()` /
  `_aggregate_identity` — the worker only feeds it rows.

## Design

### The manager

A new `LiveIdentifyManager` (module `compute/learning/live_identify.py`) mirroring
`CollectorManager`'s shape: an authoritative `running` intent flag (not derived
from `thread.is_alive()`), a fresh `Thread` + `Event` per start, a best-effort
join of any prior thread, and a `status()` snapshot for `/api/stats`. Its tick
loop is the compute analogue of `run_collector` — but it reads frames *out* of the
store rather than pumping them in.

Wired in `create_app` (`compute/api/app.py`) on
`app.state.live_identify_manager`, constructed with references to the `store`, the
`analysis_manager`, and the `training_manager` (for the yield check). The shutdown
hook stops and joins it **before** `store.close()`, alongside the existing
collector/analysis/training teardown (same load-bearing ordering — it writes the
shared connection). Intent restored from `store.get_setting("live_identify") ==
"1"`.

New endpoints `POST /api/live-identify/start` and `/stop` toggle it; `status()`
folds into `/api/stats`. A start/stop toggle lives on the Activity page (the view
whose liveness it drives).

### The tick

Interval `_TICK` (default 5 s). Each tick:

1. `model = store.active_model()`; if `None`, idle this tick (nothing to identify
   against).
2. If `analysis_manager.running()` or `training_manager.running()`, skip (yield
   the GPU). Watermark unchanged → resumes next tick.
3. Ensure the resident `Embedder` matches `model["backbone"]/["imgsz"]`; rebuild
   it (once) if they changed. The `yolo-serial` analyzer is model-independent and
   prepared once for the manager's lifetime.
4. Find **closed** motion clusters: reuse `_gap_split` over `frames.motion = 1`
   with `id > watermark` and cluster end `recv_ts < now - _VISIT_GAP_MS`. A new
   small `Store.closed_visits(since_id, now_ms)` read returns their
   `[start_id, end_id]` spans (the same clustering `events()`/`visits()` use, so
   no drift).
5. For each closed cluster span `[lo, hi]`:
   - **detect** — `run_analysis(store, <resident yolo-serial>, <stub manager>,
     since_id=lo, until_id=hi)`: writes `yolo-serial` verdicts + boxes for
     in-span frames lacking them. `prepare()` is idempotent, so the resident
     analyzer never reloads. The span covers every frame between the visit's
     first and last motion frame — **including the few `motion = 0` frames inside
     it** (a cat pausing at the flap; still within the < `_VISIT_GAP_MS` gap, so
     the same cluster). Those are detected too, deliberately: a still cat is still
     worth identifying, and those calm frames often identify best. Edge motion
     stays the *master* gate — a stretch with no cluster is never detected — but
     inside a visit, detection is not re-gated on the per-frame motion flag.
   - **identify** — `run_identify(store, model, model["gallery_path"],
     since_id=lo, until_id=hi, embedder=<resident>)`: crops + embeds detected
     frames, k=1-matches the gallery, writes `identifications` (idempotent,
     no distance threshold — "unknown" is derived at read, as today). No
     YOLO-confidence floor is applied to which boxes get embedded in v1 —
     visit-scoping already drops *isolated* phantoms; revisit only if in-cluster
     phantoms visibly pollute names.
6. Advance and persist the watermark to the max processed `hi`.

Latency from a visit ending to its name appearing ≈ `_VISIT_GAP_MS` (2 s) + up to
one `_TICK`.

### Reuse and the one refactor

- `run_analysis` is reused unchanged, passed a **persistent** analyzer instance so
  its internal `prepare()` is a no-op after the first tick.
- `run_identify` gains an optional `embedder` param: when supplied, it skips
  building/preparing a new `Embedder` and uses the resident one (a guard asserts
  the resident embedder's `backbone`/`imgsz` match `model` — a mismatch is a
  programming error, since step 3 keeps them in sync). Passing `None` preserves
  today's manual-pass behavior exactly.
- Clustering reuses `_gap_split` + `_VISIT_GAP_MS`; `events()` and its identity
  join are untouched.

### Concurrency and safety

- **Serial GPU by yielding** (step 2) rather than a shared lock — simplest correct
  model given the store already serializes DB writes under its own lock.
- **Collector untouched** — the worker only reads the store; the thin, no-ML
  collector keeps streaming and can't be starved by GPU work on another thread.
- **Idempotent** — all writes are `INSERT OR REPLACE` on their PKs, so even a
  yield-check race with a manual job is a no-op, never corruption.

### The post-promote note

`promoteModel`'s note in `compute/api/web/index.html` (~line 5601) is reframed by
worker state: worker **on** → "New visits are named automatically; run Identify to
also re-name earlier events against this model." Worker **off** → the existing
"Names in Activity will not update until you re-run Identify" text. This directly
resolves the confusion that motivated the feature.

## Alternatives considered

- **Recurring job on the existing queues (Approach B).** Re-enqueue detect+identify
  tail jobs onto `AnalysisManager`/`TrainingManager`. Rejected: pollutes the
  walk-away job history with endless jobs and competes on the same FIFO as the
  operator's manual gallery-build/validate.
- **Per-frame tail identify, no visit wait (Approach C).** Identify every detected
  tail frame the moment it lands, letting `events()` cluster on read. Rejected: it
  embeds isolated/phantom single-frame detections (wasted GPU), which is exactly
  the visit-scoping this feature is meant to provide; its only edge — instant
  naming — isn't worth it.
- **Crop from the edge motion bbox instead of running YOLO.** Would drop the
  detection pass entirely, but motion ≠ cat (non-cat motion, a loose blob box), so
  it would name non-cats. Rejected — detection is what makes a crop a *cat* crop.

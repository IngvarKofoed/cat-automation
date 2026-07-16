# Training page (learning-loop Train stage)

A new `#train` page in the compute motion-workbench SPA that gives the **Train**
stage of the learning loop (Collect → Annotate → Train → Run) a home in the UI,
so the feasibility probe stops being a memorized `python -m` incantation. The page
is structured as the three sub-steps the architecture already names — **build the
gallery → validate it → promote a version** — but only *validation* (the existing
feasibility probe) is built now; build and promote are designed here so the page
and data model are shaped to grow into them, and stubbed as visibly-disabled
sections. Validation runs as a background job on a new, dedicated **training
queue** that mirrors the proven `AnalysisManager` pattern, with live progress/ETA
and the report rendered in-page.

## Key decisions

- **New `#train` SPA page** (reuses). Adds `'train'` to `ROUTES`, a nav-link, a
  `view-train` section, and an `onRouteEnter('train')` handler — the same
  hash-router shape as `#annotate`/`#sweeps` in `compute/api/web/index.html`. No
  routing mechanism invented.
- **Dedicated `TrainingManager`** (extends). A new manager in a new
  `compute/learning/` package (the layout `docs/ARCHITECTURE.md` reserves for
  "training + promotion"), *mirroring* `AnalysisManager`'s daemon-thread + single
  lock + FIFO + finished-job history + cancel/stop-all lifecycle — but with a
  separate queue and its own `/api/training/*` surface. It is **not** the same
  instance as the oracle-sweep manager: training and sweeps are unrelated
  workflows and must not share a dedup namespace or contend for one queue slot.
- **Heterogeneous training jobs** (diverges). Unlike `AnalysisManager`, whose job
  always resolves an `Analyzer` and calls `run_analysis`, a training `_Job` carries
  a `kind` (`'feasibility'` now; `'gallery-build'`/`'promote'` later) and the
  worker dispatches on `kind` to the right run function. Progress is a generic
  `done/total` (no analyzer-specific `present`).
- **Feasibility pipeline lifted into a library** (extends). The embed → metrics →
  charts → HTML pipeline moves out of the CLI tool `compute/tools/feasibility.py`
  into an importable orchestrator in `compute/identification/` so both the CLI and
  the API call the same code. The pure-numpy metrics (`feasibility.py`) stay
  import-light; the new orchestrator lazy-imports matplotlib, matching the
  torch-gating discipline in `embed.py`.
- **Report embedded as served HTML** (new). The manager writes the same
  self-contained `feasibility.html` the tool produces, into a **timestamped
  per-run directory**; a `FileResponse` endpoint serves a run's report by id and
  the page shows it in an `<iframe>`. Native dark-theme re-render is a deferred
  follow-on, not validation-first.
- **Durable validation-run history** (new). Each successful feasibility run writes
  a `feasibility_runs` row (quality selection, crop/cat counts, kNN accuracy, AUC,
  suggested threshold, report path, timestamp), so separability over time is
  queryable and survives restart. The table survives eviction and `clear()` like
  `cats`/`dataset_items` — run history is precious, decoupled from the rolling
  frame buffer. This is distinct from, and built before, `model_version`.
- **`model_version` table** (new). The data model's `Model version`
  (draft/active/retired) is designed here but **written only once gallery-build
  exists**. A validation run produces *no* version — it measures the labelled
  data and records a `feasibility_runs` row, but builds no model.
- **`embed_paths` gains a progress + stop callback** (extends). An optional
  `progress(done, total)` callback, invoked per batch, feeds the manager's
  `set_total`/`record` for the ETA UI **and** carries the cancel signal — when the
  job's `stop_event` is set the embed loop aborts at the next batch boundary, so
  Cancel actually interrupts the long phase instead of no-op'ing until it's nearly
  done. (Decision: feasibility is cancelable.)
- **Quality A/B is a UI control** (reuses / diverges). gallery/ok/poor checkboxes
  map to the run's `qualities` (the `Store.labeled_crops` filter just added).
  Unlike a sweep — identical work over immutable frames — a feasibility run reads
  the *current* labelled set, which grows as you annotate, so an identical
  `(kind, qualities)` is **not** identical work over time. Dedup therefore guards
  only a genuine double-click (an identical request while that same job is the one
  *running*), never a pending job — a deliberate re-run after labelling always
  enqueues rather than being silently dropped onto a stale job.

## Goals

- Run the feasibility probe from the dashboard as a background job — no CLI, no
  remembered command.
- Show live progress + ETA (reusing the sweeps panel's client-side anchor logic)
  and the rendered report in-page when it finishes.
- Drive the gallery/ok/poor A/B from the UI, so "is crop quality the bottleneck?"
  is answerable with clicks.
- Keep a durable, queryable history of validation runs (metrics + report) so
  separability can be tracked as labelled data grows.
- Establish the Training page shell and the `model_version` data model that
  gallery-build and promote will slot into, without building them yet.

## Non-goals

- Building gallery-build or promote now — designed and stubbed, deferred to
  follow-on specs.
- Native/dark re-render of the report in the SPA (embed the existing HTML first).
- Fine-tuning the embedding backbone (the rare heavy path; out of scope here).
- Any run-mode, decision-engine, or actuation wiring — this is the teaching loop,
  which the concept keeps separate from the live door loop.

## Design

### Page shell & routing

`view-train` holds three stacked panels titled for the sub-steps: **1. Build
gallery**, **2. Validate**, **3. Promote**. Panels 1 and 3 render as disabled
cards with a "coming next" note so the page is honest about what works today;
panel 2 (Validate) is fully live. `onRouteEnter('train')` loads the cat roster and
the current training-job status. The nav-link sits after `Annotate`, following the
loop's left-to-right order.

### TrainingManager (the dedicated queue)

New `compute/learning/runner.py`, structurally a sibling of
`compute/analysis/runner.py`: one `threading.Lock` guarding all mutable state, a
daemon worker running one job at a time, a fresh `stop_event` per promotion, a
bounded finished-job `history`, and `cancel`/`clear_pending`/`stop_all`/`join`.
The load-bearing invariant is unchanged and copied deliberately: the "record
terminal state → clear running → promote next" transition is one atomic lock hold
in the worker's `finally`.

Differences from `AnalysisManager`, all driven by the heterogeneous-job decision:

- `_Job` carries `kind` + a params payload (for `'feasibility'`: the `qualities`
  tuple) instead of a resolved `Analyzer`; the timestamped report dir is assigned
  when the job *runs*, so it is not in the job or its dedup key. `dedup_key()` is
  `(kind, params)`, but the manager's `_enqueue` diverges from `AnalysisManager`'s:
  it dedups only against the **running** job (a double-click guard), never against
  pending jobs — see the Quality-A/B decision (a feasibility run's input grows, so
  a re-run is not a duplicate).
- `_run` dispatches on `kind`. For `'feasibility'` it calls the identification
  orchestrator, passing `self.set_total`/`self.record` and the job's `stop_event`
  as the progress + stop hook (a set `stop_event` aborts the embed loop → the job
  records as `canceled`, and no `feasibility_runs` row is written); on success it
  writes the `feasibility_runs` row (the orchestrator stays a pure compute+report
  function — persistence is the manager's concern, so the CLI tool can reuse it
  without touching the DB).
- `status()` returns `{running, kind, params, done, total, error, queue, history,
  result}`. `result` carries the most-recent finished run's `run_id` + summary
  (kNN accuracy, AUC, threshold, crop/cat counts), so a poll that arrives after
  completion can render the outcome and point the iframe at its report without a
  second fetch.

A separate manager instance is created in `create_app` (`app.state.training_manager`)
and stopped+joined in the shutdown hook, exactly as the analysis manager is.

### Feasibility as a library + the run job

New orchestrator `compute/identification/probe.py` (name TBD) with roughly:

```
run_feasibility_probe(store, out_dir, qualities=None, progress=None) -> dict
```

It does what `main()` in the CLI tool does today — `labeled_crops(qualities)` →
`Embedder.embed_paths(..., progress=progress)` → `run_feasibility` → charts +
`_render_html` → write `feasibility.json` + `feasibility.html` — and returns the
summary dict (+ report path). `progress` also carries the stop signal; when set,
`embed_paths` stops at the next batch and the orchestrator raises a cancellation
the worker records as `canceled`. It **guards the cold-start case** the CLI does
today (fewer than 2 crops or 2 distinct cats → a structured "not enough labelled
data" result, not a raw `run_feasibility` `ValueError`), so the endpoint can
surface the friendly empty-state. It does **not** touch the DB: persisting the
`feasibility_runs` row is the caller's job (the manager persists; the CLI just
prints), keeping the orchestrator a reusable pure compute+report step. The
chart/HTML helpers (`_scatter_png`, `_confusion_png`, `_hist_png`, `_render_html`)
move here from the tool. matplotlib is imported lazily inside the chart helpers,
so importing the module stays cheap. `compute/tools/feasibility.py` becomes a thin
CLI wrapper over this function (argument parsing + printing the summary),
preserving its current behavior.

The manager sets `total` from the crop count before embedding and calls `record`
per batch via the callback; embedding dominates runtime, so crops-embedded is a
faithful ETA denominator.

### Endpoints (`/api/training/*`)

- `POST /api/training/feasibility/run` — body `{qualities: [...]|null}`. Mirrors
  `/api/analysis/run`'s discipline: run `Embedder.ensure_available()`
  **synchronously** first, so a missing-deps environment fails at request time
  with the install hint (503) rather than as a delayed `status().error`. It also
  **pre-checks the labelled-crop counts** for the requested `qualities`: fewer than
  2 crops or 2 distinct cats returns the friendly "label at least two cats"
  empty-state (the guard the CLI's `main()` does today) **without enqueuing** — so
  a fresh or under-labelled store's first click is a clear next-step message, not a
  red failed job. Otherwise enqueue; returns `{**status(), position, deduped}`.
- `POST /api/training/cancel`, `/queue/clear`, `/queue/stop-all` — thin
  pass-throughs to the manager, matching the analysis controls.
- `GET /api/training/status` — the poll the page renders.
- `GET /api/training/feasibility/runs` — the `feasibility_runs` history,
  most-recent-first, each with its metrics, `run_id`, and a `report_available`
  flag (false once its report dir has been pruned), for the panel's run list.
- `GET /api/training/feasibility/report/{run_id}` — `FileResponse` of that run's
  `feasibility.html` (the iframe `src`); 404 if that run's report has been pruned
  (its metrics row still lists, flagged `report_available: false`).

### Report rendering, storage & retention

Each run gets its own timestamped directory
`<CAT_COLLECT_DIR>/training/feasibility/<ts>-<slug>/` holding its
`feasibility.{json,html}` (`<slug>` is the tier-ordered quality slug — `all`,
`gallery`, `gallery+ok`, …). The directory/timestamp is assigned **when the job
runs**, not at enqueue, so it is not part of the dedup key. `feasibility_runs`
rows are kept indefinitely (a row is tiny); the on-disk report dirs are bounded to
the most recent `CAT_TRAINING_REPORTS_KEEP` (default 25), oldest pruned after a
run — an aged-out run keeps its metrics row but its report endpoint 404s. Pruning
(and any report-dir deletion) **swallows `OSError`**, mirroring `Store._unlink`: on
the Windows compute PC, deleting a report file open in a concurrently-served
`FileResponse` raises a sharing violation that must not crash the run.

The Validate panel shows the quality checkboxes, a Run button, the progress/ETA
line (reusing the sweeps panel's ETA anchor logic but with its **own** anchor
state, not the single shared `etaAnchor` global — so a training run and a
concurrent oracle sweep can't blank each other's ETA), a **list of recent runs**
from `/runs` (each a row of headline tiles — accuracy / AUC / threshold / quality
/ time), and an `<iframe>` that loads the selected run's report (defaulting to the
newest, or the just-finished run from `status().result`). A run whose report was
pruned (`report_available: false`) still shows its tiles but renders a "report
pruned — re-run to regenerate" placeholder instead of loading a 404 into the
iframe.

### Validation run history

New `feasibility_runs` table:

```
feasibility_runs(id, ts, quality TEXT,       -- slug: 'all' | 'gallery' | ...
                 n_crops, n_cats,
                 knn_accuracy REAL, auc REAL, threshold REAL,
                 report_dir TEXT, notes)
```

Written by the manager's worker on a successful feasibility job (not by the
orchestrator). It is the durable half of the history the in-memory manager
`history` only holds until restart, and the source for the panel's run list.

### Data model: `model_version` (designed, deferred)

The second new table, matching the data model's `Model version` — and, unlike
`feasibility_runs` above, **not** built in this slice:

```
model_version(id, version, status TEXT,  -- 'draft' | 'active' | 'retired'
              kind, metrics JSON, threshold REAL, created_ts, notes)
```

Like `cats`/`dataset_items`, it survives eviction and `clear()` (a promoted model
is precious, decoupled from the rolling frame buffer). **Not created in the
validation slice** — it is written by gallery-build (produces a `draft` with the
gallery vectors + the validation-suggested threshold) and mutated by promote
(flips `draft`→`active`, prior `active`→`retired`, exactly one `active` at a time).
Documented now so the Training page and its endpoints are shaped for it.

## Settled decisions (from review)

- Page is named **"Training"** with Build/Promote as visibly-disabled "coming
  next" cards — honest about what works while keeping the loop's stage name.
- A validation run **persists** a `feasibility_runs` row (not just in-memory),
  giving durable separability history; it still produces no `model_version`.
- Feasibility and oracle sweeps **may run concurrently** — separate managers, each
  one-job-at-a-time internally; simultaneous GPU pressure is accepted for a manual,
  infrequent action rather than adding a cross-manager mutex.
- Reports are **timestamped and retained** (bounded, see retention above), not
  latest-only — so past runs stay comparable.
- The dedicated queue (A) was **re-challenged in a second review pass and kept
  knowingly**: the durable `feasibility_runs` table now covers the run history that
  was A's main edge over single-flight, but A is retained as deliberate forward
  investment so gallery-build/promote land into a ready queue.
- A feasibility run is **cancelable** — the embed loop honors the stop signal, so
  Cancel/Stop interrupt the long phase rather than no-op'ing until it's nearly done.

## Alternatives considered

- **Single-flight, no queue (Approach B).** Feasibility as one background task with
  a module-level run-state, no FIFO. Less code now. Reconsidered during review —
  the durable `feasibility_runs` table already covers run history, weakening B's
  main downside — but A was kept deliberately as forward investment for the
  gallery-build/promote jobs that will share the queue.
- **Generalize `AnalysisManager` into a generic JobManager (Approach C).** One
  queue for sweeps and training, UI branching on job-kind. Rejected — it retrofits
  a crop-embedding job onto a tightly-invarianted analyzer-and-frames class and
  couples two unrelated workflows.

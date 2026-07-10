# Motion-gate validation via offline oracles (compute)

Add a compute-side analysis layer to the frame-collection browser that runs a
stronger, slower detector over the stored frames *offline* and persists a
per-frame verdict, so the edge's MOG2 motion gate can be **validated** against a
better reference and its disagreements surfaced for eyeballing. Two oracles behind
one interface: **YOLO** (COCO cat detector — "is a cat here?") and **BSUV-Net**
(deep background subtraction — "is there foreground/motion?"). A "Run" kicks off a
background sweep; a new disagreement view — the analogue of the existing "Missed?"
preset — shows frames where MOG2 and a chosen oracle disagree. Also adds a
start/stop toggle for the collector so the store can be frozen for a clean pass.
The purpose is trust, not replacement: MOG2 stays the live gate.

## Key decisions

- **Separate `analysis` table, one row per `(frame_id, analyzer)`** (new; aligns
  with ARCHITECTURE's separation of Identification from Sighting). Keeps the
  collector's hot `frames` table untouched, and a third oracle later is new rows,
  not a migration. Verdicts are append-only observations *about* a frame.
- **`Analyzer` interface + two backends** (new). `YoloAnalyzer` and
  `BsuvAnalyzer` both reduce their output to a uniform `verdict` (bool "subject
  present") + `score` + `detail`, so one disagreement query serves both.
- **"Present" means different things per oracle, deliberately** (new). YOLO
  present = a cat was detected; BSUV present = foreground/motion was detected.
  Both collapse to a boolean for querying, but the UI labels keep the meaning
  explicit — because a MOG2-motion / YOLO-no-cat frame is *not wrong* (it's a
  leaf/person/shadow), whereas a YOLO-cat / MOG2-still frame is a genuine miss.
- **Offline background sweep, one job at a time** (extends the collector's
  daemon-thread pattern). A job runner drives an analyzer over the store; progress
  is polled; a second run request while one is active is refused.
- **Stateless vs windowed analyzers iterate differently** (new). YOLO is
  per-frame stateless → the runner drives it over frames *lacking* a verdict, so
  re-runs skip done work and resume cheaply. BSUV needs a **sliding recent
  background** (its verdict depends on temporal neighbours), so it drives over the
  *full* time-ordered set, priming its recent-frame window from the store —
  order- and resume-correct, at the cost of always revisiting every frame. Verdict
  writes are idempotent (`INSERT OR REPLACE`) either way.
- **Inference runs outside the store lock** (reuses the store's concurrency
  model). Read frame path under the lock (quick) → decode + infer unlocked (slow)
  → write verdict under the lock (quick). The always-on collector keeps writing
  `frames` while a sweep runs.
- **Heavy ML deps are opt-in** (new). `torch`/`ultralytics`/BSUV go in a separate
  `compute/requirements-analysis.txt`, lazily imported inside the backends, so the
  always-on collector still installs and runs from the lean `compute/requirements.txt`.
- **Collector becomes start/stop-able at runtime** (extends `create_app`). A small
  manager owns `(thread, stop_event, running)`; a stopped thread is replaced by a
  fresh one on restart. Enables the freeze-store-then-analyze workflow.
- **Eviction and `clear` cascade to `analysis` rows** (extends `Store`). No
  orphaned verdicts pointing at frames that retention already dropped.

## Goals

- Run a stronger offline oracle (YOLO cat-detector; BSUV foreground) over stored
  frames and persist a per-frame verdict + score in the DB.
- Surface where MOG2 disagrees with a chosen oracle — *missed* (MOG2 still, oracle
  present) and *false trigger* (MOG2 motion, oracle absent) — as one-click views,
  like the existing triage presets.
- Start/stop collection from the UI so the store can be frozen for a clean pass.
- Keep the always-on collector free of heavy ML deps.

## Non-goals

- **Condition bucketing** (luma / time-of-day) and per-condition scorecards —
  deferred; the design won't preclude them but this increment doesn't slice.
- Weather-source join.
- Re-running MOG2 offline with different params (still out of scope, per the
  frame-collection-browser spec — that reimplements the edge's stateful pipeline).
- Model versioning/promotion, training, annotation, labels — later learning phase.
- Making BSUV run on this dev box: it is CUDA-bound (TF/PyTorch research code) and
  will be tested on the real NVIDIA compute box. YOLO runs here (MPS/CPU).
- Auth (trusted LAN, per project).

## Design

### Layout (new files)

```
compute/
  analysis/
    __init__.py
    base.py         # Analyzer protocol + AnalysisResult dataclass
    yolo.py         # YoloAnalyzer  (ultralytics, COCO 'cat' class)
    bsuv.py         # BsuvAnalyzer  (deep background subtraction; CUDA)
    runner.py       # background sweep job + AnalysisManager (state/progress)
  collection/
    store.py        # + analysis table, write_analysis, disagreement query, cascade
    collector.py    # + CollectorManager (runtime start/stop)
compute/requirements-analysis.txt   # torch, ultralytics, BSUV deps — opt-in
```

### `analysis` table (`store.py`)

```sql
CREATE TABLE analysis (
  frame_id  INTEGER NOT NULL,   -- references frames.id (no FK; cascade handled in code)
  analyzer  TEXT    NOT NULL,   -- 'yolo' | 'bsuv'
  verdict   INTEGER NOT NULL,   -- 1 = subject present (cat / foreground), 0 = absent
  score     REAL,               -- max cat conf (yolo) / foreground fraction (bsuv)
  detail    TEXT,               -- JSON: boxes, model id — optional, for inspection
  ran_at    INTEGER NOT NULL,   -- compute epoch ms
  PRIMARY KEY (frame_id, analyzer)
);
CREATE INDEX idx_analysis_analyzer_verdict ON analysis(analyzer, verdict);
```

`PRIMARY KEY (frame_id, analyzer)` makes a re-run an `INSERT OR REPLACE`. All ops
go through the existing single connection + single lock. `_evict_locked` and
`clear` delete this table's rows for the affected frame ids in the same locked
critical section, so a verdict never outlives its frame.

New `Store` methods (all reuse the lock):
- `write_analysis(frame_id, analyzer, result)` — `INSERT OR REPLACE`.
- `iter_unanalyzed(analyzer, batch)` — yield `(id, path)` for frames with no row
  for `analyzer` (`LEFT JOIN ... WHERE analysis.frame_id IS NULL`), oldest-first,
  so a stateless sweep is resumable and skips done work.
- `iter_time_order(batch)` — yield every `(id, path)` oldest-first; the windowed
  (BSUV) sweep's driver, so its recent-background window stays contiguous.
- `recent_before(frame_id, n)` — the `n` frame paths immediately preceding
  `frame_id` in time order, so a windowed analyzer primes its background on
  (re)start instead of cold-starting at the resume point.
- `analysis_summary(analyzer)` — `{analyzed, present}` counts, for coverage/progress.
- `query_disagreements(analyzer, mode, cursor, limit)` — keyset-paginated (on
  `frames.id`, newest-first, same opaque-token contract as `query`). `mode`:
  - `missed` → `frames.motion = 0 AND analysis.verdict = 1`
  - `false`  → `frames.motion = 1 AND analysis.verdict = 0`

  Rows carry the same shape as `query` plus the oracle's `score`, and only cover
  frames that have been analyzed (the join's inner side).

### Analyzer interface (`analysis/base.py`)

```python
@dataclass
class AnalysisResult:
    verdict: bool
    score: float | None
    detail: dict | None

class Analyzer(Protocol):
    name: str                              # 'yolo' | 'bsuv'
    windowed: bool                         # True → must see frames in time order (BSUV)
    def prepare(self, store) -> None: ...  # heavy one-time setup per job
    def analyze(self, image) -> AnalysisResult: ...   # image: BGR ndarray
```

`prepare(store)` runs once at job start: YOLO (`windowed=False`) loads weights and
ignores `store`; BSUV (`windowed=True`) loads its network and takes the store
handle to prime its recent-frame window. `analyze(image)` returns a verdict for
the current frame; a windowed analyzer is fed frames in strict time order and
keeps its own rolling state. `cv2`/`numpy`/`torch` are imported inside the
backends only — never at `analysis` package import — so the collector path stays
CV/torch-free, matching `StreamFrame.image`'s lazy-import discipline.

- **`YoloAnalyzer`** — a large ultralytics model (`yolo11x`/`yolov8x`) at
  **imgsz ≈ 1280** and a **low** confidence threshold: recall-first, to catch
  partial top-down cats on a pass where time is cheap and misses are what we hunt.
  Weights/size/conf are env-overridable; device auto (`cuda` > `mps` > `cpu`).
  `verdict = any COCO 'cat' detection ≥ conf`; `score = max cat conf`;
  `detail = {boxes, model}`.
- **`BsuvAnalyzer`** — deep background subtraction; requires CUDA in practice.
  Keeps a **rolling window of recent decoded frames** and builds the reference
  background BSUV-Net needs from them, primed via `recent_before` on (re)start so
  a resumed sweep has no cold-start artifact. `verdict = foreground fraction ≥
  threshold`; `score = foreground fraction`. The exact BSUV variant and its
  input-tensor layout are settled at implementation on the CUDA box — the one
  remaining unknown, not a spec-blocking choice.

### Sweep runner + manager (`analysis/runner.py`)

`AnalysisManager` holds the state of the single active job: `{running, analyzer,
done, total, present, error, stop_event}`. `run_analysis(store, analyzer,
stop_event, manager)`:

1. `analyzer.prepare(store)`. Pick the iterator by `analyzer.windowed`: stateless
   → `store.iter_unanalyzed(name)` (skip done, resumable); windowed →
   `store.iter_time_order()` over the full set. Set `total` from that count.
2. For each `(id, path)`: decode the JPEG, `analyzer.analyze(image)`,
   `store.write_analysis(...)` (`INSERT OR REPLACE`), bump `done`/`present`.
   Check `stop_event` between frames so a job is cancelable.
3. On any per-frame decode/inference error, log-and-skip (like the collector),
   so one bad frame can't abort a long sweep.

One job at a time: a second run request while `running` returns 409. Inference is
unlocked; only the path read and verdict write take the store lock.

### API (`app.py`)

- `POST /api/collector/start` · `POST /api/collector/stop` → toggle via
  `CollectorManager`; return `{running}`.
- `POST /api/analysis/run` `{analyzer, reanalyze?}` → start a sweep (409 if one
  runs; `reanalyze` clears prior rows for that analyzer first). Deps missing →
  a clear 400/503 ("install compute/requirements-analysis.txt"), not a 500.
- `POST /api/analysis/cancel` → set the job stop event.
- `GET /api/analysis/status` → the manager state + per-analyzer
  `analysis_summary` (analyzed / present / coverage vs store count).
- `GET /api/frames` — extend with optional `analyzer` + `disagree=missed|false`;
  when present, route to `query_disagreements`. Existing `motion`/`order` path
  unchanged.
- `GET /api/stats` — add `collector_running`.

### UI (`web/index.html`, vanilla JS, no build step)

- **Collector toggle** — a Start/Stop button + a running/stopped badge in the
  stats panel.
- **Analysis panel** — analyzer select (YOLO / BSUV), **Run** + **Cancel**, and a
  progress badge polled from `/api/analysis/status` ("YOLO: 4 200 / 17 197
  analyzed · 37 present"). Reuses the existing 4 s poll loop.
- **Disagreement presets** — for the selected analyzer, two one-click buttons
  extending `applyPreset`: **"Missed (oracle sees subject)"** →
  `disagree=missed`, and **"False trigger (oracle sees nothing)"** →
  `disagree=false`. Labels state the oracle's meaning so a YOLO false-trigger
  (just a non-cat) reads differently from a real miss. Disagreement tiles get a
  distinct border from the plain motion border.

### Deps / entry point

`compute.sh` and `compute/requirements.txt` are unchanged — the collector stays
lean. Analysis deps install separately into `.venv-compute`
(`pip install -r compute/requirements-analysis.txt`); a "Run" with them absent
fails with a clear, actionable message rather than a stack trace. YOLO installs
and runs on this dev box (MPS/CPU); BSUV is exercised on the CUDA compute box.

## Alternatives considered

- **Verdict columns on the `frames` table (Approach A).** Simpler, but widens the
  collector's hot write table and forces a migration for every new oracle. The
  separate table is the extensible, decoupled choice for two-plus oracles.
- **Analyze live inside the collector loop.** Rejected: it couples heavy models to
  the always-on ingest path and contradicts the "trade compute + time offline"
  premise. The sweep runs over the *stored* set on demand.
- **Condition bucketing + scorecard now.** Deferred by request; the `analysis`
  table + `ran_at`/`score` leave room to add luma/time slices later without
  reshaping anything.

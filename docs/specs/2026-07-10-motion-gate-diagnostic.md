# Offline MOG2 re-run & tuning compare (compute)

Tune the edge's MOG2 motion gate **offline, on the compute side**, against the
frames already collected — instead of the slow "change the Pi, wait days,
eyeball" loop. The compute re-runs the *exact same* motion-gate algorithm the Pi
runs (shared code, not a copy) over the stored frames with adjustable parameters,
persists each re-run's per-frame `(motion, area)` beside the oracle verdicts, and
compares two re-runs as **(missed, false-trigger) scorecards against a chosen
oracle** — so you can see whether a parameter change improved recall without
inflating false triggers, on a fixed validation set. Seed a baseline run from the
Pi's current settings, run a candidate with edited params, diff the scorecards.
This is the only path that can tune `var_threshold`/`learning_rate` offline (they
change what MOG2 *measures*, so reading stored `area` can't recover them). Read-
only w.r.t. the Pi; the winning params are copied into the edge config UI by hand.

## Key decisions

- **One shared motion core, imported by both tiers** (new; expands `shared/`'s
  remit → needs an `ARCHITECTURE.md` update). Extract the edge grabber's
  post-transform motion logic (downscale → gray → MOG2 → threshold → morph →
  largest-blob → area → locality gate → persistence debounce) into a `MotionGate`
  class both the edge and the compute re-run instantiate. This is what makes the
  offline gate *identical* to the live one — a param that improves the re-run
  improves the Pi by construction — and kills the "second MOG2 that drifts"
  objection. The edge refactor must be **behavior-preserving** (its tests still
  pass unchanged).
- **The re-run is a windowed analyzer** (extends the BSUV pattern). MOG2 is
  stateful (its background model builds frame-by-frame), so the re-run must see
  frames in strict time order and carry state across `analyze` calls — exactly
  `BsuvAnalyzer`'s contract (`windowed = True`, `Store.iter_time_order`,
  warm-start). Simpler than BSUV: no CUDA, no weights, MOG2 ships in OpenCV.
- **Fair compare = re-run(old) vs re-run(new), NOT vs stored `frames.motion`**
  (new). The offline re-run can't perfectly reproduce the live gate (the Pi's MOG2
  had continuous history before collection; dropped frames aren't stored; the
  re-run downscales a JPEG'd ROI, not raw pixels). Comparing stored motion against
  a new-param re-run would conflate the param change with that reproduction error.
  So both sides of a compare are re-runs over the *same* stored frames, cold-
  started identically — the only difference is the params.
- **Stored `frames.motion` is the live-reality reference + a one-time fidelity
  check** (new). Re-run with the Pi's *current* params and compare to
  `frames.motion`: high agreement empirically validates the whole method; low
  agreement quantifies the transfer gap. Reported once, not used as the delta
  baseline.
- **Oracle stays ground truth for missed/false** (reuses the motion-gate-oracles
  layer). "Missed" = gate-still ∧ oracle-present; "false trigger" = gate-motion ∧
  oracle-absent — against the *same* oracle over the *same* frames for both
  scorecards. Textbook precision/recall tuning against a fixed validation set.
- **Re-runs persist to the `analysis` table in named slots** (extends analysis
  storage). `mog2:baseline` and `mog2:candidate`, one row per `(frame_id, slot)`,
  `verdict` = motion, `score` = area, `detail` = the params used. No schema change
  — the table already holds "an observation about a frame."
- **The scorecard is generalized over a (motion source × oracle)** (extends the
  earlier diagnostic idea; supersedes the CLI `diagnose_misses.py`). One function
  computes recall, miss confidence-split, area-vs-threshold buckets, and visit
  clustering for *any* motion source — the live `frames` column or an `analysis`
  slot — against any oracle.
- **Pi settings fetched read-only** via `EdgeClient.get_config` + a
  `GET /api/edge/config` proxy (extends the ingest client, which speaks only
  `/stream` + `/status`). Seeds the baseline run's params; degrades to
  `edge/config/settings.py` defaults tagged `source:"defaults"` when the edge is
  unreachable. A GET — nothing is written to the Pi.

## Goals

- Re-run the edge's exact motion gate offline over stored frames with adjustable
  parameters, on the compute side, with no live edge round-trip per frame.
- Seed a baseline from the Pi's current settings; run a candidate with edited
  params; **diff the two scorecards** to see if recall improved without inflating
  false triggers.
- Tune **all** knobs offline — including `var_threshold`/`learning_rate`, which
  stored `area` alone can't recover.
- Quantify how faithfully an offline re-run reproduces the live gate (fidelity
  check), so the tuning's transfer to the Pi is trusted, not assumed.
- Support proper fine-tuning against a large, condition-varied validation set (the
  multi-day collection the user is about to gather).

## Non-goals

- **No auto-push of tuned params to the Pi.** The winner is copied into the edge
  config UI by hand; closing that loop is a later step.
- **No change to live edge behavior.** The shared-core extraction is a pure
  refactor on the edge side.
- **Not a separate "area-only replay."** Re-running the real gate subsumes it
  (the earlier "Approach B" cheap replay of `min_area`/`max_area`/`persistence`
  from stored `area` is unnecessary once the full gate re-runs).
- **No new ML**, and no real-time operation — sweeps are on-demand.

## Design

### Shared motion core (`shared/motion.py`)

A `MotionGate` holding the MOG2 model + debounce streak, constructed from the six
params (`var_threshold`, `learning_rate`, `min_area`, `max_area_fraction`,
`persistence`, `downscale`), with:

- `process(roi_bgr) -> (motion, bbox, area)` — the post-transform core lifted
  verbatim from `Grabber._compute_motion` (downscale → gray → `apply` → threshold
  254 → morph OPEN → `connectedComponentsWithStats` → largest blob → area → `min ≤
  area ≤ max` → streak/`persistence`).
- `reset()` — drop the model + streak (the edge's relearn; a fresh re-run).

The edge `Grabber._compute_motion` becomes: `crop(rotate(frame))` then
`self._gate.process(roi)` — it keeps the transform (its input is a raw camera
frame); the shared core is everything after. The compute re-run calls
`gate.process(image)` directly, because stored frames are *already* rotated+
cropped (the Pi streams the ROI). `downscale` remains a tunable — it happens
inside `process`. Pure `cv2`/`numpy`; both tiers already depend on OpenCV, so no
new dependency. Lives in `shared/` because it is now genuinely cross-tier
behavior — which bends `ARCHITECTURE.md`'s "`shared/` = contracts only," so that
doc gets a short update naming this expansion (alternative homes in *Alternatives*).

### `MogAnalyzer` (windowed re-run) — `compute/analysis/mog2.py`

Satisfies the `Analyzer` protocol, `windowed = True`. Constructed with a param set
(not env vars — params come from the run request). `prepare(store)` builds a fresh
`MotionGate` and warm-starts it by replaying `recent_before` frames through
`process` (priming the MOG2 background, mirroring BSUV). `analyze(image)` →
`gate.process(image)` → `AnalysisResult(verdict=motion, score=area,
detail={params, bbox})`. Because MOG2 is deterministic given the frame sequence, a
re-run with identical params is reproducible/cacheable.

### Getting the Pi's settings

`EdgeClient.get_config()` — `GET {base_url}/api/config`, same `requests`+timeout
pattern as `get_status`, returning **all six** persisted motion params
(`var_threshold`, `learning_rate`, `min_area`, `max_area_fraction`,
`persistence`, `motion_downscale`) so the re-run can match the Pi exactly and
`downscale` is itself tunable. A `GET /api/edge/config` compute proxy serves them
to the UI as `{source:"edge", ...}` (or `settings.py` defaults as
`{source:"defaults"}` when unreachable / the collector holds a `None` client).
Edge keys `max_area_fraction`/`motion_downscale` map to the UI's
`max_area`/`downscale`.

### Per-run scorecard — `Store.gate_scorecard(source, oracle, *, warmup, thresholds…)`

`source` is either the live gate (`frames.motion`/`frames.area`) or an `analysis`
slot (`mog2:candidate`, …). Against `oracle` (`yolo`/`bsuv`), over the frames past
the `warmup` prefix (default 500 — MOG2's `history`, so the cold-started model has
stabilized; configurable, negligible over multi-day data), it returns:

- **recall** — `caught`/`present`, `missed` (source-still ∧ oracle-present).
- **false triggers** — source-motion ∧ oracle-absent.
- **miss confidence split** — missed set bucketed on oracle `score` (high ≥0.5 /
  med ≥0.3 / low), so recall-first YOLO's borderline over-calls (conf floor 0.15)
  don't read as gate faults. (For BSUV `score` is foreground-fraction — same
  buckets, generic labels, v1.)
- **area→knob buckets** — missed set by `area` vs thresholds: `< min_area` (of
  which `near_zero` = MOG2 saw ~nothing → `var_threshold`/`learning_rate`),
  `> max_area`, in-band (→ `persistence`). For a `mog2` source `area` is the
  re-run's `analysis.score`; for the live source it's `frames.area`.
- **visits** — cluster present frames by `recv_ts` gap; a visit is *caught* if any
  frame in its span (± window) had source-motion; `wholly_missed` is the count
  that actually cost a GPU trigger.

Pure SQL aggregates + one time-ordered Python scan for clustering, under the store
lock (on-demand, human-paced). Supersedes `compute/tools/diagnose_misses.py`,
whose logic folds into this function (a thin CLI wrapper may remain for headless
use).

### The tuning flow & endpoints

Sibling `/api/tuning/*` routes (kept separate from `/api/analysis/*` so the fixed
ground-truth oracles stay clean of parameterized, slotted MOG2 runs):

- `POST /api/tuning/rerun {slot, params}` — run `MogAnalyzer(params)` into
  `mog2:{slot}` (a background sweep like the oracle runner; one at a time). The UI
  seeds `baseline` from `GET /api/edge/config` and `candidate` from the edited
  fields.
- `GET /api/tuning/compare?oracle=yolo` — returns the scorecards for the live
  gate, `mog2:baseline`, and `mog2:candidate` against `oracle`, plus the fidelity
  agreement (baseline re-run vs stored `frames.motion`) and the per-metric deltas
  (Δmissed, Δfalse) baseline→candidate.

### UI

A tuning panel in the browse page (vanilla JS, reusing `.badge`/`.analysis-error`):
the six param fields prefilled from the Pi (labelled with `source`), "Run
baseline" / "Run candidate" buttons, and a compare readout showing the two
scorecards side by side with the deltas highlighted (green = fewer misses,
red = more false triggers), the oracle selector, the fidelity line, and the
warmup-excluded note. The winning params are shown for copy-paste into the edge
config UI.

## Alternatives considered

- **A — read-only diagnostic only** (the prior draft): score just the *live* gate
  against the oracle, no re-run. Useful and works on today's data, but can't tune
  the sensitivity knobs and can't preview a change against collected frames. Its
  scorecard is subsumed here as the per-run readout.
- **Motion core in `edge/motion/` with compute importing it.** Rejected: makes
  `compute` depend on `edge`, a layering inversion; `shared/` is the agreed
  cross-tier dependency even at the cost of expanding its remit.
- **Overload `/api/analysis/*` + `ANALYZER_NAMES` with mog2.** Rejected: oracles
  are fixed ground-truth references; MOG2 runs are parameterized, slotted, and
  self-compared — different enough that a sibling surface is clearer than
  special-casing the oracle machinery.
- **Auto-apply the winning params to the Pi.** Deferred: a write path to the edge
  is a bigger commitment; hand-copy for now.

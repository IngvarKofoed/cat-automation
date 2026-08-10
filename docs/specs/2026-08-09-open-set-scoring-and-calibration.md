# Open-set scoring and identification calibration

The validation probe can measure how well we name a cat we have labelled, but not
how often we name a cat we have *never* labelled — every crop it scores belongs to
a known cat. That blind spot is load-bearing: the active model declines **0 of 515**
held-out visits, so a genuine stranger today is confidently given a resident's name,
and nothing in the system would show it. This spec makes stranger rejection
measurable, makes the operating point settable without a rebuild, and answers two
open questions about the gallery — whether per-cat capping fixes the 1-NN density
bias, and whether the crop geometry fed to DINOv2 is costing us anything.

## Key decisions

- **Everything is computed in-run, against one distance matrix** (reuses). The
  stranger passes, the cap sweep and the threshold grid are all masks over the matrix
  `run_feasibility` already builds and holds in memory. No persisted embedding cache
  and no second scoring path — a cache would only buy asking a new cap value hours
  later, at the cost of a parallel code path that must stay byte-identical forever.
- **Stranger scoring is an explicit mode inverting `_score_visits`' unscoreable
  branch** (extends). Hold a whole *cat* out of the gallery via the existing
  `gal_mask`; each of its visits becomes a trial whose correct outcome is *declined*.
  The branch that currently skips these is exactly the case being scored.
- **Threshold becomes a settable property of a model version** (new). Today it is
  written once by `build_gallery` and only ever read, so the operating point cannot
  move without a rebuild. Applied at read in `Store.events()`, so a change takes
  effect with no re-identify and is instantly reversible.
- **The capped forecast reuses `cap_per_cat` and recalibrates under the mask**
  (reuses). The cap's own selection becomes a `gal_mask`, so the forecast describes
  exactly what a build would enrol — and the threshold is recomputed under that mask,
  because capping is meant to fix the calibration bias as well as the density one.
- **Crop geometry is stamped per `dataset_items` row** (new). An additive column, the
  migration path entry 322 established. Without it the *second* geometry change after
  any frame eviction leaves old- and new-geometry crops side by side with no record of
  which is which, and a later build blends feature conventions silently.
- **Geometry arms are per-run request parameters** (reuses). Carried into
  `feasibility_runs.metrics` and the report dir slug exactly as `qualities`,
  `max_per_cat` and `exclude_cat_ids` already are — a probe run produces no model
  version, so the version stamp cannot be what identifies an arm.
- **Arms are compared paired, per visit** (new). `_score_visits` emits per-visit
  outcomes; two arms are compared over discordant visits at a matched decline rate.
  The weakest cell holds 19 visits, so unpaired headline comparison cannot separate a
  real effect from noise.

## Goals

- Measure how often an unenrolled cat is given a known cat's name, split by whether
  the impersonated cat is a resident (the dangerous direction) or a neighbour.
- Produce an operating-point curve pairing decline rate on known cats against
  impersonation rate on held-out cats, and be able to apply a chosen point.
- Answer whether per-cat capping fixes the density bias, without building a gallery
  to find out.
- Answer whether letterboxing and a context margin help, with enough statistical
  power to believe the answer either way.

## Non-goals

- Night IR illumination — hardware pending, and no preprocessing recovers detail the
  sensor never captured.
- Size/shape fusion — deferred to the actuation phase, where a size *veto* belongs.
  It cannot separate the dominant confusion pair (both cats are big) in any case.
- Changing the embedding backbone.
- Automatic threshold selection at build time (see *Alternatives considered*).
- Improving the day column: 98.2% over 383 visits is ~7 errors, nearly all
  within-class.

## Design

### Stranger rejection

For each cat `C` present in the run, score a pass with `gal_mask = (y != C)`. Every
visit of `C` is then a trial with no correct name available, so:

- outcome *unknown* → **rejected** (the fail-safe answer)
- outcome *named X* → **impersonation**, recording `X`

This is an explicit **mode**, not a consequence of the mask. Under `gal_mask = (y != C)`
every one of `C`'s groups has an empty `others`, so `_score_visits` as written counts
them all `unscoreable` and `continue`s — the pass would score nothing. The mode says:
score only `C`'s groups, and treat *unknown* as correct. The cross-regime callers pass
no mode and are unchanged.

Impersonations are reported split by whether `X` is a resident or a neighbour, since
"stranger named as one of our cats" is the outcome the door cares about and "stranger
named as another neighbour" is not. That split is **not free**: `labeled_crops` selects
`cat_id`/`name` only, and `run_feasibility`'s `cats` list is `{cat_id, cat_name, n}`, so
`is_resident` has to be plumbed through both.

Held-out residents and held-out neighbours are both scored and reported separately.
A held-out neighbour simulates a genuine stranger; a held-out resident simulates a
cat that exists but has not been enrolled yet. Both are real situations at this door.
A cat excluded via `exclude_cat_ids` is absent from `labeled_crops` upstream and so is
not holdable-out at all.

The headline impersonation figure is **micro-averaged over held-out visits**, with
per-cat rows beside it — the same shape and convention `_per_cat` and the block's own
`accuracy` already use, so the curve and the rows cannot disagree. Macro-averaging over
cats would weight a three-visit cat equally with a two-hundred-visit one; the per-cat
rows are where a thin cat stays visible.

Degenerate cases report `available: false` with a `reason`, matching `_visits_block`'s
existing convention — holding a cat out of a two-cat run leaves a one-cat gallery, where
a 100%-impersonation readout would read as catastrophe when nothing was measured.

The pass sweeps the **same threshold grid** as the ordinary held-out scoring, so the
two curves pair up point for point. The grid is `_visits_block`'s `curve_ts`, computed
once on the *unmasked* pass and passed into every stranger pass — a masked pass's own
distance range differs, so a shared grid will not happen by itself:

| threshold | known-cat recall | known-cat declines | impersonations (resident) |
|---|---|---|---|

This is what an operating point is chosen against, replacing `_best_threshold`'s
balanced accuracy over pair distances — which is what parked the active model at
0.436 and zero declines.

### Threshold as a setting

`POST /api/training/models/{id}/threshold` takes `{threshold: float | null}` and
updates `model_versions.threshold`. `null` restores the uncalibrated fail-safe, which
already names nobody. A number is **bounded** — `Field(ge=0.0, le=2.0)`, cosine
distance's range, mirroring `/api/lighting`'s existing validation. Unbounded, a
mistyped `4.36` for `0.436` puts every cat *and every stranger* below the cutoff, which
is precisely the failure this spec exists to prevent, applied silently across all
history.

Two things are stamped beside the write, because both outlive the run that justified
them:

- `metrics.threshold_built` — the build's own value, copied from the current
  `threshold` column **when absent**. No existing row has it, so the first override on
  today's active model would otherwise destroy the built value with nothing recording it.
- `metrics.threshold_source_run_id` — the feasibility run the operating point was read
  off. The Model page already has the stamp-and-dim pattern for exactly this; without
  it a threshold survives with no way to see which run's grades and exclusions justified
  it.

The operator surface is a numeric field and an Apply control on the Model page's
version row, showing **built and effective side by side**. It carries an explicit
warning that the threshold is read-time: changing it restates the household's Visits
and Activity figures retroactively, across all history, with no re-identify pass and no
other indication that anything moved.

`build_gallery`'s own derivation is deliberately unchanged — it keeps stamping
`_best_threshold`'s value as the version's starting point. A build rule is inherited
by every future gallery, where a setting is reversible, so the operating point moves
by hand until the curve has been read on real data.

### Capped-gallery forecast

`run_feasibility_probe` gains `max_per_cat`. It is applied by running
`cap_per_cat(kept_labels, cap)` and turning the surviving rows into a `gal_mask` — the
same selection a build would make, so the forecast is not a second policy that could
disagree with one.

The threshold is **recomputed under each mask**, over cross-visit same-cat pairs drawn
from the surviving columns. `cap_per_cat`'s docstring names two biases — the dominant
cat's vectors blanketing the embedding space, *and* the suggested threshold being
calibrated from a distance distribution those cats' pairs dominate — and a forecast
reusing the uncapped threshold would answer only the first. Both the recalibrated and
the fixed-threshold column are reported, so which half of the bias moved is visible
rather than inferred.

One subtlety worth recording: under leave-one-visit-out, the held-out visit's columns
are masked anyway, so if the cap happened to select crops from the held-out visit,
that cat's effective gallery for that fold sits slightly *below* the cap. This
under-represents the true cat, so the forecast can only understate a capped gallery's
performance — the fail-safe direction.

Because the cap is a mask over one matrix, a single run forecasts several caps. The
run takes a list, defaulting to `[None, 2000, 1000, 500]`.

### Crop geometry

Two changes in `Embedder._embed_items`. An absent geometry stamp means **legacy**
(squash resize, margin 0), and `Embedder`'s own default stays legacy — flipping the
default instead would silently mismatch every already-promoted gallery against its own
queries, which is the exact failure the `backbone`/`imgsz` stamps exist to prevent.

- **Letterbox** replaces `cv2.resize(img, (imgsz, imgsz))`: aspect-preserving resize
  to fit, then pad to square. Pad with the ImageNet mean, which is zero after
  normalisation — black would inject a large constant into every vector. Real boxes
  range from 191×46 to 538×298, so the current squash distorts by up to 4.8× and
  differently per frame within one visit.
- **Context margin**: expand the box by a fraction before `_clamp_box`. Expected to be
  weakly negative — every cat shares one fixed doorway, so extra context is
  common-mode and dilutes between-cat separation — but it is the only way tail tips
  and extended paws stop being clipped, and it is cheap to measure.

Geometry is selected **per run**, as request parameters on the feasibility enqueue,
carried into `feasibility_runs.metrics` and the report dir slug exactly as `qualities`,
`max_per_cat` and `exclude_cat_ids` already are. A probe run produces no model version,
so the version stamp cannot be what identifies an arm — and without the run row naming
its own geometry, the paired comparison below is not auditable after the fact.

Stored crops are the artifact that survives frame eviction, so **`dataset_items` gains
a `geometry` column** recording how each crop was cut. `labeled_crops` returns it and
`build_gallery` filters on it, so a gallery is always built from one convention. A
geometry change re-cuts what it can via the existing `materialize` — 100% of labelled
crops currently resolve to a live frame — and stamps each re-cut row; anything it
cannot re-cut keeps its old stamp and is simply excluded from builds at the new
geometry. Without the column the second such change after any eviction would leave a
silently mixed store, which is the failure this avoids.

`build_gallery` embeds stored crops via `embed_paths` while `run_identify` cuts from
frames via `embed_crops`, so gallery vectors carry one more JPEG generation than the
queries matched against them. The probe uses stored crops on both sides and has never
seen this. An `embed_crops`-at-margin-0 arm measures the delta directly, and the
asymmetry is accepted if that arm puts it below the noise floor: routing the gallery
through frames would retire it outright but reintroduces a mixed-source gallery for
crops whose frames have evicted, which is a worse property to carry permanently than
one JPEG generation.

### Comparing arms honestly

`_score_visits` gains a per-visit outcome list: `{cat_id, first_src_recv_ts, outcome,
named, night}`. Two arms score the same visits, so a comparison joins on
`(cat_id, first_src_recv_ts)` and counts discordant visits, tested with McNemar.

The list is written to the **run dir**, not into `feasibility_runs.metrics`. Metrics
rows are kept indefinitely and `/api/training/feasibility/runs` parses 100 of them on
every Model-page load; 515 visits × 5 fields per run would grow both without bound for
data only a paired comparison reads. The cost is that a comparison needs both run dirs,
which `prune_feasibility_reports` bounds at 25.

Two plumbing constraints this carries. `cat_id` and `named` must be the **real** cat
ids, resolved through `cats` the way `_per_cat` already does — inside `_score_visits`
they are positional indices over the cats present in *that* run, so excluding a cat
shifts every index and two arms' "cat 3" are different cats (entry 357's trap). And
`first_src_recv_ts` cannot come from `feasibility.py`, which is a deliberately
pure-numpy layer holding no timestamps: the probe passes a parallel per-group metadata
list alongside `visit_groups`.

Three rules that follow from the measured shape of the data:

- **Compare at a matched decline rate.** Each arm recalibrates its own threshold and
  `accuracy` is `correct/decided`, so an arm can "win" purely by declining more. The
  curve each arm already produces is what the comparison reads.
- **Judge on the paired all-visit result and the cross-class error count**, not the
  Store Sultan ↔ Store Jihn cell. That cell is 13 of 23 errors but entirely
  foreign→foreign; only 5 errors cross the resident/foreign line, 4 of them in the
  stranger-let-in direction.
- **Always print the discordant counts and the McNemar p-value**, and never declare an
  arm the winner without them. A bare "letterbox wins" is printable off three
  discordant visits, which is exactly the reading Goal 4 exists to prevent.

`docs/ARCHITECTURE.md` needs a matching edit: it describes a training run as producing
"a new model version, carrying its own suggested threshold", which a settable threshold
plus `threshold_built` no longer fully describes.

## Alternatives considered

- **A persisted embedding cache plus a `rescore` entry point.** Would let a new cap
  value be asked hours after a run without re-embedding. Rejected: every question in
  Goals 1–3 is a mask over a matrix the run already holds, a preprocessing arm needs a
  fresh embed regardless, and paired comparison comes from the per-visit outcome list —
  so the cache bought convenience in exchange for a second scoring path to keep
  permanently in sync.
- **Threshold policy moved into the build.** Makes the calibration fix permanent
  rather than a setting, but commits to a policy before the curve has been seen on
  real data, and every future build inherits it. Deferred — revisit once the operating
  point has been chosen by hand a few times.
- **Stranger scoring over neighbours only.** Halves the run's cost and keeps the
  number narrowly interpretable, but drops the held-out-resident case, which is
  exactly what enrolling a new cat looks like.
- **Separate day/night galleries.** Already measured and closed: night-vs-night is
  86.4% while the mixed gallery gives night 87.9%, so splitting buys nothing.
- **Size/shape fusion now.** Cannot separate the dominant confusion pair, and its real
  job — refusing a resident's bigger foreign lookalike — has no consumer until the
  door lock exists.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Multi-agent, 3 streams.** (1) Scoring core — `identification/feasibility.py`,
  `identification/probe.py` and the report: stranger mode, cap sweep, per-visit
  outcomes. (2) Threshold setting — the store method, `POST
  /api/training/models/{id}/threshold`, and the Model-page field. (3) Crop geometry —
  `identification/embed.py`, the `dataset_items.geometry` column and its re-cut,
  `build_gallery`'s filter. Then one integration pass.
- **Opus 5 on streams 1 and 3, Sonnet 5 on stream 2.** Stream 1 interprets the
  inverted-branch and recalibration semantics; stream 3 migrates the
  eviction-surviving labelled artifact, where a mistake costs labels. Stream 2 is an
  endpoint and a field, fully specified down to the bounds and the stamps.
- **Shared seam to coordinate:** streams 1 and 3 both add a column to
  `Store.labeled_crops` (`is_resident` and `geometry`).

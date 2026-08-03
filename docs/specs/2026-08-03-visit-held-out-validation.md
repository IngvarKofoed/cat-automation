# Visit-held-out validation

The feasibility probe currently reports ~100% kNN accuracy while the gallery it
forecasts scores 0.804 balanced accuracy. The cause is that its leave-one-out masks
only the diagonal (`feasibility.py:120`), so a crop's nearest neighbour is almost
always the adjacent frame of its *own* visit — a near-duplicate. This adds a second
scoring to the same probe run: group the labelled crops into visits, hold out a
whole visit, match it against a gallery built from the *other* visits, and score at
the **visit** level using Run's own threshold-and-vote rule. Day and night are
scored separately, plus a cross-regime matrix that tests whether one gallery can
span both. Measurement only — the identify path is untouched.

## Key decisions

- **A second scoring inside the existing probe, not a new run** (extends). The
  expensive part is embedding the crops; `run_feasibility` already builds the full
  N×N cosine distance matrix, and held-out scoring is that *same* matrix with
  per-visit blocks masked. Both scorings come from one embed, one report, one
  `feasibility_runs` row — no mode selector, and no "which mode was run 6?".
- **Held-out by masking, not by rebuilding a gallery per visit** (extends). Masking
  a visit's own columns to `+inf` and taking the row-wise argmin is exactly
  `gallery.match`'s k=1 nearest-neighbour semantics, at no extra embedding cost.
  Rebuilding a `Gallery` per visit would re-run the same maths hundreds of times.
- **The visit verdict reuses `Store._aggregate_identity` verbatim** (reuses). Run's
  rule — gate on threshold, plurality vote among below-threshold frames, else
  *unknown* — is already a static method over `[(cat_id, distance)]`. Calling it
  means the probe cannot drift from what Run actually does.
- **Visits are grouped by `src_recv_ts` gap, deliberately COARSE** (extends). Uses
  the shared `Store._gap_split` primitive, but with its own
  `_HELDOUT_GAP_MS = 60_000` rather than `_VISIT_GAP_MS` (2 s). Coarse is the
  fail-safe direction here: over-merging two visits removes *more* leakage, while
  under-merging splits one physical visit across the held-out boundary and lets the
  near-duplicates back in — the exact defect being fixed.
- **`labeled_crops` returns `src_recv_ts`** (extends). It already returns
  `src_frame_id`; the timestamp is what both the grouping and the day/night
  bucketing key on. Reading it off `dataset_items` (not `frames`) means the scoring
  covers every label, including those whose frames have since been evicted.
- **Day/night via the injected `is_night` callable** (reuses). Same
  dependency-injection seam `cat_regime_coverage` and `gate_scorecard` use, so the
  store stays astral-free and an unset location degrades to "split unavailable"
  rather than guessing a boundary.
- **Visit metrics persist in a new `feasibility_runs.metrics` JSON column** (new).
  Mirrors `model_versions.metrics`. The table has no migration machinery — schema is
  `CREATE TABLE IF NOT EXISTS` only — so this introduces the repo's first additive
  `ALTER TABLE ... ADD COLUMN`, guarded by a `PRAGMA table_info` check. Existing rows
  read NULL and render as "not measured", never as a zero.
- **The report's headline becomes the visit-level number** (diverges). Crop-LOO stays
  in the report, demoted, with one line saying why it reads high — but it stops being
  the tile a reader sees first, and the verdict sentence is driven by the visit-level
  accuracy instead. Entry 314 established the old headline is misleading; leaving it
  in the lead position would preserve exactly that.

## Goals

- Produce a validation number that survives the near-duplicate objection, scored at
  the unit Run actually decides on (a visit), with Run's own rule.
- Say whether identification works **at night**, separately from day.
- Answer whether one gallery can span both regimes, or whether day and night need
  separate galleries — currently an open question in `ARCHITECTURE.md`.
- Reuse the already-labelled crops: no re-sweep, no new collection, no re-annotation.

## Non-goals

- **Changing identification.** No change to `run_identify`, the live worker, gallery
  build, or promotion. This only measures.
- **Size as a discriminator.** Queued separately in `docs/TODO.md`; it is a mechanism
  change, and it needs this ruler to exist first in order to be scored at all.
- **Per-model-version validation.** Validation still scores the labelled *data*, not a
  built artifact (see `CHANGELOG` 207/209). A run still belongs to no model version.
- **Retiring the crop-LOO scoring.** It stays, demoted — it is the one number
  comparable with the five existing runs.
- **Scoring several grade selections in one run.** The run's grade selection stays its
  single scope, exactly as today, so a run remains identified by what it measured.
  Comparing `gallery` against `gallery+ok` means comparing two runs, which the runs
  table already supports.

## Design

### Grouping crops into visits

`labeled_crops` rows come back ordered by `(cat_id, id)`. Group them **per cat**: sort
each cat's rows by `src_recv_ts` and run `Store._gap_split(rows, _HELDOUT_GAP_MS,
ts_of=src_recv_ts)` over that one cat's rows. Clustering the whole set globally would
merge two cats that were at the door in the same minute into a single group, which has
no well-defined true `cat_id` to score against. Two cats cannot share one *frame*'s
label (`dataset_items` is UNIQUE on `(src_frame_id, src_recv_ts)`), but adjacent frames
a few seconds apart can carry different cats, so mixed groups are reachable — and
tailgating is an expected case at this door, not a corner one.

Holding out one cat's group while another cat's simultaneous crops stay in the gallery
is deliberate and costs no leakage: a different cat's crop can only produce a *wrong*
match, never a falsely correct one.

`dataset_items.labeled_ts` is stamped once per commit (`add_dataset_items`), so every
crop of one label keypress shares it exactly. That gives a free cross-check: the
report states both the gap-derived group count and the distinct-`labeled_ts` count.
They should be close; a large divergence means the gap constant is wrong for this
door's frame rate, and it is visible rather than silent.

### Scoring one held-out visit

For a group `G` of crop indices, over the existing `dist` matrix:

```python
d = dist[G, :].copy()
d[:, G] = np.inf            # the visit cannot match itself — the whole point
nn = d.argmin(axis=1)
span = [(int(ids[j]), float(d[i, j])) for i, j in enumerate(nn)]
verdict = Store._aggregate_identity(span, threshold, cat_names, cat_residents)
```

`cat_names` comes from the rows `labeled_crops` already returns; `cat_residents` is
passed empty, since `_aggregate_identity` uses it only to populate an `is_resident`
field this scoring ignores — correctness is decided on `cat_id`.

`verdict` is then compared to `G`'s true `cat_id`, giving one of three outcomes per
visit: **correct**, **wrong** (named the wrong cat), or **unknown** (no crop fell
below threshold). Reporting *unknown* separately from *wrong* is the point — for a
resident they have opposite consequences at the door, and collapsing them into one
"error" number would hide that. So the headline accuracy is
`correct / (correct + wrong)`, and the unknown rate is its own tile beside it rather
than folded in: two numbers, neither hiding the other.

A group whose cat has **no other group** in the matrix is *unscoreable*: the correct
answer is structurally absent, so it can only be wrong. Those are excluded from the
denominator and reported as their own count with the cats named — Store Kali is one
today (17 crops, one visit). Counting them as failures would understate the model and
hide the real cause; dropping them silently would repeat the "an empty danger set
reads as safe" trap.

### Threshold

**Revised during implementation.** The plan was to state the headline at the run's own
suggested threshold. That threshold is unusable here: `_best_threshold` calibrates on
*all* same-cat pairs, and those are dominated by same-visit near-duplicates sitting at
near-zero distance, so the optimum is dragged far below what cross-visit matching needs.
Measured on a synthetic fixture with the real shape (40 crops per visit): crop-level
0.00063 against cross-visit 0.99934 — a 1587× gap, at which **every** visit is declined
and the report reads 0 correct / 0 wrong / 100% unknown. The gap is data-dependent (with
cleanly separable cats the two coincide, since the threshold is then set by the largest
same-cat distance either way), which is exactly why it cannot be assumed benign.

So the visit threshold is calibrated on **cross-visit same-cat pairs only** — the geometry
of the task being scored — reusing the `pair_d`/`same_pair` arrays `run_feasibility` has
already materialised rather than re-deriving the upper triangle (a ~580 MB allocation at
12k crops). The cross-visit AUC is reported beside it.

The circularity is still not hidden: the report carries a **curve** of visit accuracy and
unknown-rate across the sweep, with the crop-level threshold and the active model's
promoted threshold both marked on it — so the gap between calibrations is visible, and
"what would Run have done today" is readable off the chart without coupling the run to a
model version.

If `_best_threshold` yields `None` — no same-cat or no different-cat pairs survive — the
visit scoring reports **unavailable** rather than running. `_aggregate_identity` with a
`None` threshold resolves every visit to *unknown* by its uncalibrated fail-safe, which
would render as 0 correct / 0 wrong / 100% unknown: a catastrophic-looking result where
the honest reading is that nothing was measured. Fewer than two groups in total is
handled the same way.

### Day/night and the cross-regime matrix

Each group is bucketed whole by `is_night(src_recv_ts)` of its **first** crop — the
same first-frame rule `gate_scorecard`'s visit split uses, so the two cannot disagree
about which side of dusk a visit sits on.

Beyond scoring day and night separately, the same masked matrix answers the
one-gallery-or-two question by additionally masking the gallery side by regime:

|  | vs day-only gallery | vs night-only gallery | vs mixed gallery |
|---|---|---|---|
| day visits | ✓ | cross | ✓ |
| night visits | cross | ✓ | ✓ |

If night→night is strong but night→day collapses, the regimes are separable spaces
and `ARCHITECTURE`'s escalation (separate galleries selected by time of day) is the
answer — more night collection would not fix it. If night→night is *also* weak, it is
a data problem and collecting is the right response. Cells whose gallery side is empty
for a cat report as unavailable, not as zero.

### Report and persistence

`_render_html` gains a visit-level section above the existing crop-LOO material: the
headline tiles (visit accuracy / unknown rate / n visits scored), the
threshold-sweep curve, the regime table, and a visit-level confusion matrix. The
existing three charts and the per-cat table stay, under a heading that says the
crop-level numbers read high because same-visit neighbours are near-duplicates.

`run_feasibility` returns a new `visits` key alongside `knn` and `distances`;
`run_feasibility_probe` lifts its headline into the summary dict, and
`_run_feasibility` passes it to `add_feasibility_run` as the new `metrics` JSON. The
admin Model page's validation-run table gains a visit-accuracy column, rendering "—"
for the five pre-existing rows and naming the absence rather than showing a blank
(the behind-backend lesson of entries 164/173/183).

The visit-level headline is labelled as **not comparable** with the five earlier runs'
headline. It will read lower than their 99–100%, and a reader who takes that as the
model having got worse would draw exactly the wrong conclusion from the change.

## Known limits

- **The distance matrix is O(N²) and already large.** `run_feasibility` builds a full
  float64 N×N matrix, plus a copy and an argsort of the same shape, so the 12,041
  gallery-grade crops already cost ~1.2 GB per array. All 33,191 labelled crops would be
  ~8.8 GB each and would likely exhaust memory. That is the practical reason a run stays
  scoped to one grade selection. Chunking the matrix would remove the ceiling and is
  deliberately out of scope here.
- **Held-out scoring does raise the peak, contrary to what this section first claimed.**
  Measured (maxRSS, separate processes, 384-d vectors): n=4000 1034 MB → 1513 MB, n=6000
  2109 MB → 2439 MB. The per-group slices are indeed small; the cost is two pair-length
  `int64` gathers (`gid[iu]`, `gid[ju]`) for the cross-visit pair mask, ~580 MB each at
  12k crops. The largest single addition — a boolean-index copy of the whole matrix to
  find its min/max — has been removed, since `dist` is `1 - (unit @ unit.T)` and therefore
  finite, making an in-place `dist.min()/max()` exactly equivalent. The two gathers remain:
  they are what vectorises the cross-visit mask, and they sit alongside the pre-existing
  pair-length `pair_d`/`same_pair` arrays of the same order.

## Alternatives considered

- **A separate run mode + report**, leaving today's report byte-identical. Rejected:
  its only real benefit is continuity of a headline we have established is
  misleading, and it costs a selector, a schema column to distinguish modes, and two
  reports to compare by hand.
- **Grouping by `labeled_ts` alone.** Exact per commit, but a physical visit split
  across two commits (reachable — `loadQueue` filters decided frames *per frame*, so a
  returned cluster can mix) would land in two groups and leak. Kept as a cross-check
  rather than the primary grouping.
- **Rebuilding a real `Gallery` per held-out visit** via `build_gallery`/`match`.
  Faithful to the production code path, but re-embeds nothing new and repeats the same
  arithmetic hundreds of times; masking the shared matrix is the same maths.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** One thread through five files — the scoring in
  `feasibility.py` defines a metrics dict that `probe.py`'s report, the
  `feasibility_runs` row, the API, and the admin column each consume in turn. Nothing
  splits into independent streams, and parallelism would only produce agents guessing at
  a contract that isn't written yet.
- Opus rather than Sonnet because the value of the whole feature is that the number is
  *right*: a subtle masking or indexing slip yields a plausible-but-wrong accuracy, which
  is the exact failure mode this exists to eliminate.
- `feasibility.py` is deliberately torch-free, so the visit scoring, the grouping, and
  the degenerate cases can all be pinned with synthetic vectors and no GPU — build them
  test-first there before touching the report.

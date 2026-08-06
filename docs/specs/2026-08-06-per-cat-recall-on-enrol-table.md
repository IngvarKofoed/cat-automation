# Per-cat recall on the "Cats to enrol" table

The validation probe already scores every cat individually — a per-cat visit confusion
matrix, and the same matrix again per regime — but the only place that reaches the
operator is a table inside the HTML report, one click away and detached from any
decision. The runs table shows a single household-wide accuracy, so "more annotations
each day, but the number doesn't move" has no answer on the page where the enrolment
choice is made. This adds per-cat **visits scored, recall, declined, and day/night
recall** to the *existing* "Cats to enrol" table on the Model page.

The enabling change is small: `_score_visits` already computes the confusion; it starts
returning a per-cat summary alongside it, which rides the existing `visits` block into
storage with no change to the persistence path at all.

## Key decisions

- **Per-cat results are computed in `_score_visits`** (extends). It returns `per_cat`
  beside the `accuracy` it already derives from the same counts
  (`compute/identification/feasibility.py:169-178`). Because
  `_visits_block` spreads that return into the visits block
  (`feasibility.py:263-266`) and each `regimes[name]` *is* a `_score_visits` return, one
  addition lands per-cat data at `visits.per_cat` and `visits.regimes.{day,night}.per_cat`
  at once. Chosen over deriving it in JavaScript from a persisted index map so the
  derivation sits next to the number it must agree with, and so each row self-identifies
  by `cat_id` — retiring the positional-index coupling rather than re-entrenching it.
- **No change to the persistence path** (reuses). `_run_metrics`
  (`compute/learning/runner.py:98`) already persists the whole `visits` block, and
  `run_feasibility_probe` already lifts it into its summary. Nothing in `runner.py`,
  `probe.py` or `store.py` moves.
- **No new endpoint** (reuses). `loadRuns()` (`index.html:4124`) already fetches the runs
  payload on the Model page and re-fetches it when a training job finishes.
- **Columns join the existing table, not a new card** (extends). `catsTable()`
  (`index.html:3804`) gains three columns. That card's hint already states *"recall tracks
  visits, not crops, so a cat can hold the most crops and still be the worst
  recognised"* — it names the missing number; this supplies it.
- **Recall keeps the headline's convention, with declined beside it** (reuses).
  `correct / (correct + wrong)`, declined reported as its own share — rendered
  `69% · 12%` with the second figure dim, the shape `runsTable`'s `visitCell` already
  uses one card away. Folding declines into recall would make the per-cat column
  disagree with the run's own headline.
- **Comparability is stamped and dims the columns; staleness is noted and does not**
  (new). Grade or exclusion mismatch greys the recall. A dataset that has merely *grown*
  since the run gets an informational note instead — it would otherwise dim on the very
  next label, and watching that number grow is the point of the feature.
- **Latest qualifying run only; no trend, no delta** (new). Nothing historical can be
  back-filled, so a trend renders empty for weeks. The delta is a one-line addition once
  two comparable runs exist.
- **Per-cat day/night in; per-cat cross-regime out** (extends). The regime split falls
  out of the same `per_cat` addition. The `cross` cells (night visits against a day-only
  gallery) stay a run-level reading.

## Goals

- Name the cat with the weakest visit recall at a glance, on the card where the
  enrol/cap decision is made.
- Distinguish a cat weak *at night* from one weak overall — the labelling instruction
  differs.
- Distinguish *wrong* from *declined* per cat: for a resident at the door they mean
  opposite things, and they imply different fixes.
- Make unscoreable cats visible as a state of their own: a cat with one visit has its
  true answer structurally absent from the gallery, so no number about it can exist until
  it visits again.
- Never let a frozen number be read as describing the current grade/exclusion selection.

## Non-goals

- A trend or sparkline over runs, and a run-over-run delta.
- Per-cat cross-regime cells (this cat's night visits against a day-only gallery).
- Back-filling existing runs. A re-run measures today's labels, not the set an old row
  scored, so those rows stay "not measured" permanently.
- Changing what the probe *measures*. This returns and surfaces existing arithmetic.

## Design

### What `_score_visits` returns

It already accumulates `conf[true_cat, predicted_or_unknown]` per held-out group and an
`unscoreable` tally. It gains a `per_cat` list, one entry per cat that appears in the
run, each self-identifying:

```
{cat_id, cat_name, scored, correct, wrong, declined, recall, unscoreable}
```

with `recall = correct / (correct + wrong)`, or `None` when nothing was decided, and
`scored = correct + wrong + declined` (the confusion row sum). `unscoreable` is the
count already tracked separately, folded onto the cat's own entry rather than left as a
parallel index-keyed list.

Identity comes from the `cats` list `run_feasibility` already builds
(`[{cat_id, cat_name, n}]` over the same `uniq` that defines the confusion index,
`feasibility.py:431-435`), passed down through `_visits_block`. It is constructed before
`_visits_block` is called, so no reordering is needed.

The existing keys are untouched, so every current consumer — the report renderer, the
runs table, the `available: false` branches — is unaffected.

### The five cell states

The recall cell must not collapse distinct situations into one dash:

| State | Renders | Meaning |
|---|---|---|
| Scored | `69% · 0%` | Real recall, with the declined share beside it. |
| Nothing decided | `— · 100%` | Held out and matched, but every visit fell beyond the threshold. The dash is honest — no visit was decided — and the 100% is what explains it. |
| Unscoreable | `— · —` + `1 visit` | In the run, but every visit was held out with no peer to match against. |
| Absent from the run | `— · —` + `not in run` | Excluded from that run, or labelled since it ran. |
| Run predates `per_cat` | whole column `—` | No per-cat data on the row. |

Consistent with the runs table: `—` means "not measured" and a real zero is a dimmed
digit. Every dash carries either an adjacent figure or a per-row note saying which dash
it is.

### Comparability

The stamp under the table names the run and its parameters:

> From validation run 05/08-2026 06:59 · gallery · −Store Kali · +412 crops labelled since

The recall columns **dim**, and the stamp gains a reason, when either differs from the
current selection:

- the run's grades ≠ the Build grade ticks, or
- the run's exclusions ≠ the current untick set.

Both sides must be normalised first, because neither is stored in the form the
comparison implies:

- **Grades.** The run stores `quality` as a `+`-joined tier-ordered slug from
  `_quality_slug` (`probe.py:56`) — `"gallery+ok"`, or `"all"` for no filter — while
  `getQualities('b')` returns an array or `null`. Normalise the UI side through the same
  tier order and compare the slugs literally. `"all"` is **not** interchangeable with
  `"gallery+ok+poor"`: an explicit grade filter excludes NULL-quality crops
  (`store.labeled_crops`, `store.py:5756`), so the two select genuinely different sets
  and a run recorded under one did not score the other.
- **Exclusions.** `_run_metrics` omits `excluded_cat_ids` entirely when empty, while
  `getExcluded()` returns `null` for that state. Compare as sorted id lists with
  absent/null/empty all meaning "excluded nobody" — otherwise the columns dim
  permanently in the most common configuration, which is also the one where the numbers
  are most trustworthy.

Both axes count, not grades alone: an exclusion changes which cats each held-out visit
was matched against, so it moves *every* cat's recall.

Dim rather than hide — the numbers remain the best available reading, and hiding them on
each checkbox toggle would flicker the column during exactly the fiddling the table
exists for. The run is *not* searched backwards for a comparable one; a stale comparable
run misleads worse than a fresh mismatched one that says so.

**Staleness never dims.** The stamp reports `+N crops labelled since` from the run's
`n_crops` against the live enrollable counts, as information. Exact equality would
misfire anyway — the run's count is post-exclusion and post-decode-failure — and a false
dim is its own harm.

### The three empty stamps

Three states have no run to name, and must not look alike:

- **No validation run recorded** — "No validation run yet — run Validate above." The
  normal state on a fresh install.
- **The runs fetch failed** — `loadRuns()` swallows errors by design, which would
  otherwise render identically to "no run". It keeps the last-good columns and shows a
  red note, mirroring how `loadEnrollable` already handles its own failure (and via
  `classList`, never `className`, per entry 214).
- **Runs exist but none carries `per_cat`** — "No run since this landed — re-run
  Validate. If a fresh run still shows this, the compute PC is on an older build." Naming
  only the benign cause would repeat entries 164, 173 and 183: the dev proxy serves a
  local admin-next against a remote compute, so the page routinely runs ahead of its
  backend.

### Day/night

One column showing `98% / 94%`, read from `regimes.day.per_cat` and
`regimes.night.per_cat`. Each side's scored count goes in the tooltip: a cat with 30 day
visits and 1 night visit renders `97% / 100%`, and without the counts that 100% is
indistinguishable from a solid night result — in the column that exists precisely to
find weak-at-night cats.

Note the regime cells score against the **full mixed gallery** (`gal_mask=None`,
`feasibility.py:303`), so a cat with one night visit and many day visits is scoreable at
night, matched against its day crops. Unscoreable in a regime cell still means the cat
has one visit *in total*. The "one night visit, nothing to match it against" reading is
`cross.night_vs_night`, which is a non-goal.

`regimes` is `null` when the run had no location set — the column then renders `—`
throughout, with the stamp saying *that run* had no day/night split. Phrased about the
run, not the present: a location set since would make a present-tense stamp wrong.

### Row order and low-n cats

Rows keep **roster order**. Sorting weakest-first would put the cat needing labels on
top, but these rows carry enrol checkboxes and the recall arrives on a later tick than
the crop counts — a table that reorders under the pointer as data lands is a misclick
hazard. The weakest cat is found by reading the column.

A cat with two scored visits can only read 0%, 50% or 100%. It is shown anyway, with the
Visits count beside it doing the hedging. A minimum-visits cutoff would invent a
threshold nothing else on the page has, and would add a sixth meaning to a cell that
already carries five.

### Sequencing

The latest qualifying run is held in a module-scoped variable beside `enrollable`;
`renderCats()` reads whichever is present and re-renders when either lands, so no
ordering produces a blank or stale column.

- **A grade tick re-renders but does not refetch.** It changes no run — only the
  comparability verdict, which is client-side. Refetching per checkbox would walk back
  entry 201, which stopped polling that whole-table read on the shared store connection.
- **`loadRuns` gains a sequence guard**, matching `loadEnrollable`'s `catsSeq`. It
  previously wrote only into `#mRuns`; now that it also drives the recall columns, two
  overlapping fetches (mount plus a finished job) landing out of order would paint an
  older run's recall under the newer stamp.

### Result

```
Cats to enrol  (applies to Validate and Build)
──────────────────────────────────────────────────────────────────────
Enrol  Cat            Crops  Commits   Visits  Recall      Day / Night
  ☑    Mittens        3,102       41       38  97% · 0%    98% / 94%
  ☑    Store Jihn     2,725       34       31  69% · 0%    81% / 52%
  ☑    Sultan         1,940       28       26  92% · 0%    93% / 90%
  ☐    Store Kali        88        2    1 vis  —  · —      —  / —

From validation run 05/08-2026 06:59 · gallery · −Store Kali · +412 crops since
```

The Visits cell mirrors `runsTable`'s `visitsCell` convention — `N` with a dim `+M` for
unscoreable — so the same quantity is not counted two ways one card apart.

## Alternatives considered

- **Persist a `cats` index map and derive per-cat in JavaScript.** Smaller backend
  surface, but it re-entrenches the positional-index coupling in a second consumer and
  puts the derivation one process away from the headline it must not contradict — the
  same drift argument that rules out a dedicated endpoint.
- **Its own per-cat card under Validate, driven by selecting a run.** Exact provenance
  and any past run inspectable, but two cards from the enrol decision and hidden until
  clicked — weaker as the at-a-glance instrument that motivated this.
- **Expand-in-place on a runs-table row.** Cheapest surface and perfect provenance, but
  collapsed by default, which defeats the purpose.
- **A backend endpoint returning per-cat recall.** The Model page already holds the runs
  payload, so an endpoint would be a second path to the same numbers.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Two files — `compute/identification/feasibility.py` (the
  `per_cat` return, threaded through `_visits_block`) and
  `compute/api/web/admin-next/index.html` (`catsTable` / `renderCats` / `loadRuns`) —
  and the frontend can't be written until the payload shape is fixed, so there is no
  independent stream to split off.
- Opus rather than a cheaper tier because the comparability normalisation and the
  five-state cell are judgment, not transcription: both encode distinctions
  (`—` vs a measured zero, dim vs hide) the rest of this codebase is strict about.
- Verification is the compute subtree's usual pair — pytest over `_score_visits`'s new
  return, then the Playwright MCP pass on the Model page against a seeded store, since
  four of the five cell states need contrived data to reach.

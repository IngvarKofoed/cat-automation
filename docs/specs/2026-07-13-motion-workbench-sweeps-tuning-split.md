# Split the motion workbench into Sweeps and Tuning

Split the single overloaded `#motion` view in `compute/api/web/index.html` into two
hash-routed pages along the ground-truth-vs-evaluation seam: **Sweeps** produces oracle
verdicts (the "ruler"), **Tuning** evaluates the MOG2 gate against those verdicts. On
Tuning, the single oracle `<select>` becomes a coverage-driven **multi-select** that
compares the gate against each chosen oracle side-by-side in a compact visit-recall
matrix, while a single **focus** oracle drives the per-frame drill-down (density timeline
+ visit inbox). No backend changes: the multi-oracle matrix fans out N existing
`/api/tuning/compare?oracle=X` calls from the client.

## Key decisions

- **Two routes, split by workflow** (extends `ROUTES`/`setRoute`). `#motion` splits into
  `#sweeps` (oracle sweeps + job queue) and `#tuning` (MOG2 tuning + timeline + inbox).
  Reason: "kick off batch jobs and walk away" and "sit and analyze" are different cadences;
  the shared page is the clutter the TODO calls "pollution." Matches the reskin spec's
  three-view intent, which had drifted back into one page.
- **`#motion` redirects to `#sweeps`** (new). `setRoute` normalizes the retired hash so old
  bookmarks land on Sweeps rather than falling through to `start`.
- **Shared scope, mirrored in both pages** (new). One `activeScope`/`drillScope` state
  (unchanged) backs a bucket `<select>` on *each* page; changing either mirrors to the other.
  Reason: the normal flow is "sweep bucket X, then analyze bucket X" — picking it once should
  carry across.
- **Coverage-driven oracle chooser** (extends `/api/analysis/coverage`). On Tuning the oracle
  list is checkboxes built from the *already bucket-scoped* coverage endpoint: an oracle with
  `analyzed == 0` in the bucket is disabled ("no data here"), and one with `present == 0` gets
  a quiet "sees no subjects here" hint. This is exactly the TODO's "multi-select the oracles
  the bucket has data on" — no new endpoint.
- **Multi for the matrix, single focus for the drill-down** (new). The checked set feeds a
  compact **visit-recall matrix** (gate-source rows × selected-oracle columns). A single
  *focus* oracle (a radio within the checked set) drives the density timeline, the visit
  inbox, *and* the detailed scorecard cards. Reason: visits are oracle-*defined* (present
  frames), so overlaying two oracles on the timeline/inbox is muddy, and rendering the full
  card breakdown × N oracles overwhelms the page.
- **No backend change; client fan-out** (reuses `/api/tuning/compare`). The matrix issues one
  `?oracle=X` request per checked oracle in parallel and reads the `live/baseline/candidate`
  visit-recall from each. The endpoint stays single-oracle. Accepted cost: the gate re-run is
  recomputed once per oracle; bucket scoping keeps it cheap (see Open questions for the
  All-frames case).
- **Queue lives on Sweeps; Tuning shows only progress** (reuses). Full queue controls
  (cancel / clear / stop-all / history) stay on Sweeps. Tuning's "Queue baseline/candidate"
  buttons still enqueue onto the same FIFO and surface progress via the existing
  `tuningProgressBadge`, so the tweak→run→see loop stays on one page without duplicating the
  queue UI.

## Goals

- Separate producing oracle verdicts from evaluating the gate, so neither page carries the
  other's machinery.
- Compare the gate against multiple *trusted* oracles side-by-side (the YOLO vs YOLO-serial
  vs BSUV A/B `yolo-serial` was added for).
- Drive the Tuning oracle options from what the selected bucket actually has data on.
- Keep the MOG2 tune→run→see loop on a single page.
- No backend changes, no change to sweep/queue/scorecard mechanics.

## Non-goals

- **No archive/hide-oracle feature.** Hiding a known-bad oracle from *every* selector
  (including Sweeps) is a separate, complementary idea — deferred. This spec solves the
  clutter via the page split and coverage-driven options, not by hiding.
- **No server-side multi-oracle endpoint.** `/api/tuning/compare` keeps its single `oracle`
  param; the client fans out. (A `?oracles=a,b,c` batch is a possible follow-up — Open Q.)
- **No change to Start / Activity / Buckets**, the oracle registry, the scorecard math,
  warm-up handling, or the "All frames" default.
- **No new oracles.**

## Design

### Routing

`ROUTES` (`index.html:1438`) becomes `['start','activity','buckets','sweeps','tuning']`.
`setRoute` (`:1652`) normalizes `'motion' → 'sweeps'` before the include check so the retired
hash redirects. Nav (`:951–954`) replaces the one "Motion Detection" link with two:
**Sweeps** and **Tuning**. The single `#view-motion` section (`:1166–1325`) splits into
`#view-sweeps` and `#view-tuning`; `setRoute`'s `.view`/`view-<route>` toggle already handles
any number of sections.

`onRouteEnter` (`:1661`) gains a `sweeps` branch (load groups, refresh coverage + queue) and
a `tuning` branch (load groups, `loadTuningDefaults` once, build the oracle chooser from
coverage, then the matrix + timeline + inbox). Today's `reloadMotionView` splits into
`reloadSweeps` (coverage + queue) and `reloadTuning` (oracle chooser + matrix + timeline +
inbox).

### Sweeps page (`#view-sweeps`)

Relocated verbatim from `#view-motion`, behavior unchanged: the bucket scope selector + badge
+ motion-only banner, the **Oracle sweeps** panel (`buildCoverageRows`/`renderCoverage` over
all `ANALYZERS`, enqueue, re-analyze), and the **Job queue** panel (`renderQueuePanel` + the
control buttons). All oracles are listed here — this is the menu of what to run, not a verdict.

### Tuning page (`#view-tuning`)

- **Scope + oracle chooser.** The bucket `<select>` (mirroring shared scope) plus a checkbox
  list built by a new `renderOracleChooser(coverage)` from `/api/analysis/coverage` for the
  selected bucket. Each row: a checkbox (disabled when `analyzed == 0`), the label, an
  `N/total · P present` readout, and a focus radio. `present == 0` adds the "sees no subjects
  here" hint. New state: `selectedOracles` (Set of ids) and `focusOracle` (id | null).
  - **Lifecycle.** On each bucket change, rebuild from that bucket's coverage: check every
    oracle with `analyzed > 0` and set `focusOracle` to the first checked (by `ANALYZERS`
    order). A **fresh (unswept) bucket** has all rows disabled → `selectedOracles` empty,
    `focusOracle = null` — this is expected, not an error (Goal 3). Toggling the focus radio
    is constrained to the checked set; unchecking the focus promotes the next checked one, and
    unchecking the last leaves `focusOracle = null`.
  - **No empty-oracle requests.** `/api/timeline`, `/api/visits`, and `/api/tuning/compare`
    all 400 on an unknown/empty `oracle`. So requests never send an empty param: when
    `focusOracle` is null they fall back to `ANALYZERS[0].id` (`'yolo'`) — a *valid* id — so
    the timeline still renders density (its overlays just come back empty), the inbox shows its
    normal "no visits" empty state, and the matrix shows its own empty state (below). No red
    error banner on a fresh bucket.
- **MOG2 Tuning panel.** Param inputs + Queue baseline/candidate + Refresh (unchanged
  wiring). Below it, two result regions:
  - **Comparison matrix** (new, `renderScorecardMatrix`): rows = Live / Baseline / Candidate,
    columns = each checked oracle, cells = **visit-recall %** (the headline metric from
    changelog 46). Populated by `Promise.allSettled` over `loadCompareFor(oracle)` — one
    `/api/tuning/compare?oracle=X` per checked oracle, scoped to `activeScope`. `allSettled`
    (not `all`) so one oracle's failed compare renders as an error cell in *its* column rather
    than blanking the whole matrix. A Baseline/Candidate row whose slot is unrun
    (`needs_rerun`) renders **"not yet run"** in that cell, matching the detail card's existing
    treatment. When `selectedOracles` is empty the matrix shows an empty state ("Check an
    oracle above to compare — sweep one first on the Sweeps page if none have data.").
  - **Detail cards** (reuses `renderTuningCompare`): the full Live/Baseline/Candidate cards —
    missed frames, false triggers, area-vs-knob buckets, deltas, fidelity, warm-up note, and
    the copy-params row — rendered for the **focus oracle only**, from that oracle's compare
    response (already fetched for the matrix).
- **Density timeline** + **Visit inbox.** Relocated, driven by `focusOracle` in place of the
  old `motionOracleSelect.value` (`loadTimeline` `:2827`, `loadVisits`), with the empty-focus
  fallback above so density always renders even on an unswept bucket (the missed/false overlays
  are simply empty until a sweep exists). The timeline drill-down (`drillScope`) is unchanged.

### Event wiring

Replacing the removed `motionOracleSelect` `change` handler (`:2614`): checking/unchecking an
oracle re-runs the matrix fan-out (`renderScorecardMatrix`) live; changing the focus radio
reloads the timeline, inbox, and focus detail cards live. No "Refresh" click is needed for
either — the existing `tuningRefreshBtn` remains as a manual re-fetch. Changing the bucket on
*either* page updates the shared scope and resets `drillScope` (a Tuning-only drill window),
so entering Tuning after a Sweeps-side bucket change can't show a drill window that no longer
lies inside the selected bucket.

### Polling

`pollAnalysisStatus` (`:1729`) currently refreshes coverage only on `route === 'motion'`.
It now branches: on `sweeps`, refresh the coverage rows + queue panel (live during a sweep);
on `tuning`, refresh the oracle chooser's coverage (so checkboxes enable as a sweep completes)
and, on the existing job-complete trigger, reload the matrix + focus detail + timeline + **visit
inbox** (`loadVisits`) — the inbox and timeline are paired everywhere else, and a completed
sweep changes which visits are missed/false — instead of the old single
`loadTuningCompare`/`loadTimeline`.

### State summary

Removed: `motionOracleSelect` (element + its `change` handler at `:2614`). Added:
`selectedOracles: Set`, `focusOracle: string`. Kept: `activeScope`, `drillScope`,
`effectiveWindow`, `tuningDefaultsLoaded`, `lastCandidateParams`. Both pages' bucket selectors
read/write the same scope state.

## Alternatives considered

- **Reorganize in place (no split).** Add the coverage-driven multi-select + matrix to the
  one Motion page. Smallest change, but leaves the two workflows stacked — the clutter the
  TODO is about. Rejected.
- **"Queue vs read" split** (the TODO's original phrasing): all batch kickoff on page 1
  including MOG2 baseline/candidate, all results on page 2. Cleaner produce/consume, but the
  MOG2 param→run→scorecard loop ping-pongs across two pages while iterating. Rejected in favor
  of the ground-truth/evaluation seam, which keeps the tuning loop intact.
- **Everything multi-select (stacked timeline).** One timeline strip per oracle; but the visit
  inbox still needs a single oracle to define "worst-first," so it falls back to a focus oracle
  anyway — more complexity for a muddier result. Rejected.

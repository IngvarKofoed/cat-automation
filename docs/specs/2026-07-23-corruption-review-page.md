# Corruption review page

A new admin page (nav: Activity · **Corruption** · Buckets) to inspect per-frame
flags — **motion**, the new **corruption** flag, and optionally **cat-present** —
over a chosen time range. Its job is to validate and calibrate the corrupt-frame
guard (`docs/specs/2026-07-23-corrupt-frame-motion-guard.md`): see where the guard
fires on real ROI frames, and above all surface the fail-non-safe danger case — a
frame flagged *corrupt* that also holds a *cat*. Corruption is produced by a new
persisted `corruption` analyzer (a batch sweep over stored frames, reusing the
oracle/job-queue machinery), so the page reads a cheap stored flag and can filter
and count over the whole range.

## Key decisions

- **Corruption is a persisted analyzer, not computed on-the-fly** (extends).
  `_is_corrupt` needs the full ROI pixels (a thin line vanishes at reduced
  resolution), so deriving the flag means a full JPEG decode per frame. Doing that
  once in a batch sweep — the same cost shape as the YOLO/BSUV sweeps that already
  decode every frame, but CPU-only — and storing the verdict beats re-decoding on
  every page load. It also enables the load-bearing feature: *filtering* the range
  down to corrupt (and corrupt∧cat) frames, which on-the-fly can't do without
  decoding everything.
- **`corruption` is a NON-registered analyzer** (diverges). It is built and handed
  to `AnalysisManager.enqueue_analyzer` like `MogAnalyzer`, and is **not** added to
  `ANALYZER_NAMES`. Reason: `ANALYZER_NAMES` drives the gate scorecard
  (`_SCORECARD_ORACLES`), the disagreement view, and the oracle-coverage loop —
  corruption is not gate ground-truth about cats/motion, so registering it would
  wrongly make it selectable there. The page queries it by the literal name
  `"corruption"`; `analysis_coverage("corruption", …)` already accepts any name.
- **New `CorruptionAnalyzer` wraps `shared.motion._is_corrupt`** (new). Stateless
  per frame (no windowing, unlike MogAnalyzer/BSUV), so it rides the stateless
  sweep path. `analyze(image)` → `AnalysisResult(verdict=is_corrupt, score=None,
  detail={reason, thresholds})`. Single source of truth — the exact function the
  live gate runs — so a swept verdict equals the live verdict for that frame.
- **Verdict detail stamps the `_CORRUPT_*` constants** (extends). Mirrors how
  `MogAnalyzer` stamps its params. This gives cheap **staleness detection**: the
  page compares a stored verdict's stamped thresholds against the current module
  constants and warns "verdicts predate a constant change — re-sweep," instead of
  silently showing stale flags after the constants are retuned.
- **Recompute via the existing `reanalyze` flag** (reuses). Changing the constants
  and re-sweeping with `reanalyze=true` clears + recomputes the window's
  `corruption` verdicts, exactly as the Activity "Analyze" backfill does for
  `yolo-serial` (changelog 90/91).
- **New page reuses the Buckets range picker + the browse feed** (reuses). The
  date/time → id-range uses `/api/frames/resolve`; the frame list is the existing
  keyset-paged `/api/frames` (which already returns per-frame `motion`), scoped by
  `since_id/until_id`, joined to the `corruption` (and `yolo`) verdicts. Thumbnails
  are `/media/{id}` (full ROI, displayed small). Hash route `#corruption`.
- **Sweep triggered from the page, progress on Sweeps** (reuses). A "Compute
  corruption for this range" button enqueues the sweep on the shared job queue,
  scoped to the resolved range; progress shows on the Sweeps page — mirroring the
  Activity "Analyze" pattern, so no new queue/progress UI.

## Goals

- Let the operator see, over any time range, which real ROI frames the guard flags
  corrupt — the vehicle for the corrupt-frame guard's deferred **resolution
  calibration** (its constants were set on downscaled full-frame previews).
- Surface the danger case first-class: frames flagged **corrupt AND cat-present**,
  which would be a resident-suppressing (fail-non-safe) miss.
- Give a corruption **rate/count** over a range (a rising rate = a degrading
  cable — the hardware-health signal from the guard spec).
- Reuse the analysis/sweep/feed machinery; add no new queue, no new storage shape.

## Non-goals

- **Live constant-override tuning in the page.** The page shows swept verdicts at
  the *baked* constants; sweeping candidate threshold values without editing code
  is the on-the-fly CLI calibration tool's job (separate, deferred).
- **A decimated whole-range overview.** The first cut is the paged, filterable
  feed (the frame-hunting need); a sampled density-over-time strip is a deferred
  nice-to-have.
- The live edge exposing corruption on the wire/`/status` — still deliberately off
  the wire (that stays a future LAN-saving decision).
- A general per-frame everything-viewer. This page carries motion + corruption +
  (optional) cat-present only; identification/subject are out of scope.
- Auto-recompute when constants change — staleness is *detected and warned*, the
  re-sweep is a manual `reanalyze`.

## Design

### Backend

`compute/analysis/corruption.py` — `CorruptionAnalyzer` implementing the analyzer
protocol: a no-op `prepare()`/`ensure_available()` (numpy is always present in the
compute env), and `analyze(image)` returning `AnalysisResult(verdict=_is_corrupt(
image), score=None, detail={"reason": "cast"|"line"|None, "thresholds": {…}})`.
`_is_corrupt` gains a tiny public seam so the analyzer imports it without reaching a
private name, and returns *which* check fired (cast vs line) for the detail —
otherwise its logic is unchanged.

Enqueue path: a request resolves the range, then `enqueue_analyzer(
CorruptionAnalyzer(), name="corruption", since_id, until_id, reanalyze, motion_only)`
— the same call `MogAnalyzer` uses. Verdicts land in `analysis` under
`analyzer="corruption"`. `iter_unanalyzed`/coverage/`reanalyze` all key on the
analyzer name and so work unchanged.

Reads: a range-scoped feed that joins each frame to its `corruption` verdict and
(where present) its `yolo`/`yolo-serial` verdict, plus a `filter` for `all` /
`corrupt` / `corrupt-and-cat`. This mirrors the analyzer-join already in
`query_disagreements`; a corrupt-only or danger filter is a `WHERE` on the joined
verdict. Coverage for the range = `analysis_coverage("corruption", since,until)`.

### Frontend (`compute/api/web/admin`)

A `#corruption` view: the Buckets-style start/end date+time picker → resolves to an
id range → a paged, filterable frame grid. Each frame is its thumbnail plus a
**flag bar** (the user's design): small per-flag cells, each flag its own colour,
lit when set — **motion**, **corruption**, **cat** (where a yolo sweep covered it).
A filter control (all / corrupt / corrupt∧cat) and a header readout: corruption
coverage + count + rate over the range ("312/40,110 frames corrupt · 0 with a
cat"), and the staleness warning when stored thresholds ≠ current constants. The
corrupt∧cat filter operates over whatever `yolo`/`yolo-serial` verdicts exist in
the range, and the header flags incomplete cat-coverage — so an empty danger set
reads as "none found so far," never as a false "safe" when the range is un-swept.
Clicking a frame opens it full-res (the stored JPEG is full ROI) so a flagged thin
line — invisible at thumbnail size — can be confirmed.

## Alternatives considered

- **Compute corruption on-the-fly per page load.** Live-accurate and zero storage,
  but a full JPEG decode of every displayed frame every load (seconds), and it
  cannot filter the range to corrupt-only. Kept as the model for the CLI tuning
  tool, not the page.
- **Register `corruption` in `ANALYZER_NAMES`.** Would auto-wire it into coverage
  loops, but also into the scorecard and disagreement views where it is
  meaningless (it isn't gate ground-truth) — so it's a non-registered analyzer
  instead.
- **Extend Activity playback / Buckets with a red corrupt border.** Lighter, but
  can't host the filters or the range-wide count, and mixes calibration into a
  user-facing view. Not precluded as a later addition.

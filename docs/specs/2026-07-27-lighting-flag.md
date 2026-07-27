# Day/night lighting flag

Record, per stored frame, a continuous **colourfulness statistic** that separates
colour-daylight from IR-monochrome night, swept offline over stored frames and stored in the
`analysis` table. The threshold that turns that statistic into a `day`/`night` label is
applied **at read time**, not at sweep time — so the flag can be swept now, before the NoIR
camera is fitted, and calibrated later from the recorded distribution without re-sweeping
anything.

Motivating context is in `docs/NOIR_SWAP.md`: the IR illuminator has its own photocell, so
the moment the lighting actually flips does not track sunrise/sunset, and today's
`suntimes.py`-based day/night split will mis-bucket the transition once IR is in play.

## Key decisions

- **Statistic, not verdict** (extends). The sweep stores a continuous colourfulness value
  in `analysis.score`; the threshold lives in settings and is applied when reading. This is
  the `oracle_floor` pattern from changelog 72 — "re-slicing the SAME stored verdicts, no
  re-sweep". It is what makes "we cannot calibrate the threshold yet" a non-problem rather
  than a compromise. `CorruptionAnalyzer` sets `score=None` because its guard has no
  continuous confidence; this one does, and that is the whole point.
- **Non-registered `LightingAnalyzer`** (reuses). Clones `compute/analysis/corruption.py`
  exactly: stateless (`windowed = False`, so it rides the resumable per-frame path), kept
  **out of** `ANALYZER_NAMES` so it never leaks into the gate scorecard, disagreement view,
  or oracle-coverage loop, and constructed directly for `AnalysisManager.enqueue_analyzer`.
  Its rows land under the literal analyzer name `"lighting"`.
- **Classifier lives in `shared/motion.py`** (reuses). Beside `classify_corruption`, for the
  same stated reason: a pure, dependency-light function that compute uses today and the edge
  will need identically later, so there is never a second implementation to drift
  (the `MotionGate` lesson, changelog 22). Numpy-only — no ML extras, runs anywhere.
- **Uncalibrated resolves to `day`, carrying a `calibrated` flag** (new). With no threshold
  set, a *measured* frame reads `day` — factually right for the whole current store, and it
  gives consumers a usable label from the first sweep. The payload still carries
  `calibrated: false` so the validation surfaces can annotate it, keeping "assumed day" and
  "measured day" distinguishable where that matters (changelog 157) without forcing every
  reader to handle a null. An **unswept** frame is a different case and still resolves to
  `null` — there is no statistic behind it at all.
- **The All/Day/Night control scopes SCORING, never the re-run's frame set** (new). MOG2 is
  stateful — `MogAnalyzer` walks frames in order building a rolling background and
  warm-starts from just before the window (changelog 26). Filtering frames to one lighting
  state would shatter that background across every gap and produce garbage verdicts. The
  re-run always walks the whole window; only the scorecard's visit population narrows.
- **The selector runs on `suntimes.py` at first** (extends). The scorecard's existing
  day/night split stays sun-time-driven until the statistic is calibrated; the selector is
  wired to that split today and switches source later. Today there is no IR at all, so sun
  times are still *correct* — they only become wrong once the photocell is in the loop.
- **Downscale to a FIXED constant, not `MotionParams.downscale`** (diverges).
  `classify_corruption` runs on the full un-downscaled ROI because it hunts 1-few-row lines;
  lighting is a global property, so this one downscales first for speed (a full-day sweep is
  ~10⁵–10⁶ frames). It must use its own module constant rather than the tuning parameter:
  the statistic's value depends on the resolution it is measured at, so reading
  `motion_downscale` would let a MOG2 tuning change silently invalidate an already-calibrated
  lighting threshold.
- **The analyzer name is a one-way door** (new). `"lighting"` becomes a value in
  `analysis.analyzer`, half of `PRIMARY KEY (frame_id, analyzer)`. Renaming it after rows
  exist orphans every verdict without a migration — the `yolo-serial` lesson (changelog 147),
  where the slug had to stay despite being operator-visible. Renaming is free now, before any
  code exists, and expensive immediately after.

## Goals

- Produce a per-frame signal that says whether the frame is colour-daylight or IR-mono,
  measured from the pixels rather than derived from the clock.
- Make it calibratable *after* the fact: sweep now, pick the threshold when NoIR frames
  exist, with no re-sweep — and give the operator enough of the distribution to pick it.
- Validate the false-positive side **before** the camera arrives, using the current store
  as a negative control (see Design → Negative control).
- Let motion tuning report visit recall for day or night alone, so the headline number
  reflects the lighting being tuned.

## Non-goals

- The edge computing or using the lighting flag live. Nothing on the Pi changes.
- Two MOG2 parameter sets, or any lighting-driven parameter switching.
- Hysteresis / debouncing. That is a property of *switching*, which is out of scope; the
  per-frame statistic is deliberately raw, and a future span layer can smooth it.
- Repointing the scorecard's day/night split from `suntimes.py` to the measured flag. That
  waits for calibration — swapping to an uncalibrated source would empty the night column,
  which is strictly worse than today.

## Design

### The statistic

`shared/motion.py` gains `colourfulness(roi_bgr) -> float` and `lighting_version() -> int`,
mirroring the `classify_corruption` / `corruption_thresholds` pair.

Under IR on a NoIR sensor the scene collapses toward R≈G≈B; in daylight it does not. The
complication is that a *locked* white balance (see `docs/NOIR_SWAP.md` item 1) leaves a
strong constant cast on IR frames, which a naive chroma measure would read as colour. So
each channel is normalised by its own mean before the spread is taken, which removes any
global cast and leaves only *object* colour variation:

```
per-channel mean normalise  ->  per-pixel (max - min) across the 3 normalised channels
                            ->  mean over pixels
```

Normalising by the per-channel mean makes the channels dimensionless (each ~1.0), so the
result is a **relative** spread, not a 0–255 one: 0.0 is perfectly monochrome and a colourful
scene lands in the low tenths. Fixing the units matters because the threshold is a bare
number in settings — an implementation that rescaled back to 8-bit would be self-consistent
but silently change what a saved threshold means.

- Non-3-channel input returns `0.0` — definitionally monochrome, mirroring
  `classify_corruption`'s mono bypass.
- A near-black frame has channel means at ~0 and no meaningful normalisation, so it also
  returns `0.0`. That is honest for the statistic but ambiguous for the label: **a dark frame
  with the IR lamp off looks monochrome too.** Rather than resolve that now, the sweep
  records mean luminance alongside, so a two-axis rule stays available later without a
  re-sweep.

`AnalysisResult` per frame:

- `score` — the colourfulness value.
- `verdict` — `False` always. The row exists to carry `score`; there is no boolean truth to
  record here, and the read path never consults it.
- `detail` — `{"luma": <mean luminance>, "version": lighting_version()}`. `version` stamps
  the formula so a changed definition is detectable as stale, the same job `thresholds` does
  in the corruption verdict.

### Reading it

`Store.get_lighting_threshold` / `set_lighting_threshold` on the existing settings KV,
matching the `get_location` / `set_location` pair (changelog 122), plus
`GET`/`POST /api/lighting`.

Resolution is a pure read-time function of `(score, threshold)`:

| swept | threshold | score | lighting | `calibrated` |
|---|---|---|---|---|
| yes | unset | any | `day` | `false` |
| yes | set | `< threshold` | `night` | `true` |
| yes | set | `>= threshold` | `day` | `true` |
| no | — | — | `null` | — |

An uncalibrated frame reads `day` — true of the entire current store — so consumers get a
usable label immediately rather than a null to special-case. `calibrated: false` rides
alongside so the validation surfaces can say *why* it reads day; the two are collapsed only
in the label, never in the payload.

An **unswept** frame stays `null`. That is not the same state: uncalibrated-but-measured has
a real statistic behind it, unswept has nothing, and a sweep that hasn't run must never
present as a measurement (changelog 157).

Rows are frame-keyed, so they evict with their frames exactly as `analysis` and
`identifications` already do — the flag is a property of a stored frame, not durable output
like `dataset_items`, and needs no eviction handling of its own.

`GET /api/frames/sample?flags=1` gains `lighting` and `colourfulness` beside the existing
`motion` / tri-state `corrupt` / `area` markers (changelogs 157, 174). Additive: absent
`flags` the payload stays byte-identical.

### Where the sweep is started

A new **Lighting** card on `/admin-next#tuning`, a sibling of the existing YOLO card and
identical in shape — deliberately its own card rather than a row inside one headed "YOLO",
which would misfile an unrelated analyzer's Run button. It carries, for the day selected in
the 4-week calendar:

- **Coverage** — `x / y` frames swept, from `/api/analysis/coverage`'s additive `slots`
  field (changelog 134 — already the home for non-oracle analyzers, deliberately outside
  `oracles`/`ANALYZER_NAMES`).
- **Run lighting** + a **Rerun all** checkbox (`reanalyze`), the latter being what re-sweeps
  after a `lighting_version()` change.
- Its own **queue table**, category-filtered, with the standard progress / FPS / ETA /
  Cancel row (changelog 136).
- A **coarse distribution** of the day's colourfulness values — a small histogram over
  rounded `score`, one `GROUP BY`. This is what makes the threshold pickable: two separated
  modes is the signal that IR night is distinguishable, and where they separate is the
  threshold. Without it, "calibrate it later" has no surface to do it on.
- The **threshold field** itself, with Save. Measure, see the distribution, set the value —
  all in one place.

The sweep is **day-scoped**, enqueued over the selected day's id window; resumability,
cancellation, and `reanalyze` all come from `run_analysis` unchanged. A calendar day spans
both lighting states, so one sweep yields both populations — which is what calibration
needs. No whole-store backfill; sweeping more days is how you get more data.

The calendar cells gain a **LIGHT** percentage beside the existing YOLO/BASE/CAND ones, and
the day-stats panel a matching coverage figure — so which days have been swept is visible
without selecting each in turn, the same role the other three already play.

### Negative control (do this before the camera arrives)

The current IMX708 has an IR-cut filter and there is no IR illuminator, so **nothing in the
existing store is ever IR-mono** — even the darkest 3 a.m. frames are noisy colour. Sweeping
today therefore has a known-correct answer: the distribution should be unimodal and entirely
on the colour side, with no frame reading as monochrome.

If dark night frames come out near zero, the statistic is measuring darkness rather than
monochromaticity and needs the luma term promoted into the rule. This is the half of the
validation that *cannot* be done once NoIR is fitted, which is the argument for building
this now rather than after the swap.

### Frame review

Tiles gain a lighting marker, joining the motion/corrupt outlines and the area chip
(changelogs 159, 175). It renders the raw colourfulness value alongside the label, and while
`calibrated` is false it marks the label as assumed rather than measured — the same posture
Frame review already takes on area, where it "states the number and makes no judgement".
This is the surface for eyeballing whether the flag flips when the LEDs actually come on.

### The All/Day/Night selector

A three-way control on the motion-tuning scorecards. `All` is the default and today's
behaviour, and it **resets to `All` on every page load** — a scoring filter that silently
survives a reload is an easy way to misread a scorecard days later.

Selecting `Day` or `Night` narrows the **visit population** the scorecards are computed
over — recall, missed, false triggers, and the headline visit-recall footer all become that
state's numbers. The re-run's frame set is untouched (see Key decisions). The missed-visit
panel follows the selection, so the list can't contradict the headline above it.

Bucketing reuses `gate_scorecard`'s existing per-visit rule — a visit is assigned by its
**first present frame** (changelog 123) — so a dusk-straddling visit is attributed exactly
as the current split already attributes it, and `All` continues to equal day + night.

That split needs a location, and `?split=1` already "reports it unavailable" when none is
set rather than guessing a boundary (changelog 123). So with no location the `Day`/`Night`
options are **disabled**, with a note pointing at the Start page's location setting — a
control that silently scores nothing is worse than one that says why it can't.

## Alternatives considered

- **A `lighting` column on `frames`, written at ingest.** Sits beside `motion`/`area` and is
  cheaper per frame, and it is the natural home if the edge eventually ships the value in the
  wire format. Rejected for now: backfilling the existing store needs a migration plus an
  UPDATE sweep, and a bare float column carries no stamp of which formula produced it —
  `analysis.detail` does, which matters while the definition is still settling.
- **`lighting_spans` — store transitions, not frames.** Follows `mode_changes` (changelog 32)
  and `purge_spans` (126): two rows a day instead of ~10⁶, and hysteresis falls out naturally
  because a span has a start and an end. Rejected because it bakes the threshold at write
  time — re-picking it means re-deriving every span, and it discards the per-frame
  distribution calibration depends on. Not closed off: spans can be derived from the
  per-frame statistic later, once the threshold is known, and that is likely the right shape
  for driving live switching.
- **Filtering the re-run's frames by lighting** rather than scoping the scoring. Rejected on
  correctness, not taste: MOG2's rolling background cannot survive the resulting gaps.
- **Resolving an uncalibrated frame to `null`** rather than `day`, per the changelog-70
  convention that an uncalibrated threshold names nothing. Rejected deliberately: unlike an
  identification, a wrong lighting label admits no foreign cat and drives nothing yet, and
  `day` is factually true of the entire pre-NoIR store. The honesty the convention protects
  is preserved by the `calibrated` flag instead of by a null.
- **Naming it "regime".** The docs use the word for the day/night split, but it is jargon
  next to the UI's plain "Day"/"Night", and it abbreviates badly on a calendar cell.
  `lighting` names the thing that actually changes — sun versus IR lamp.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Five files on one dependency chain — the statistic in
  `shared/motion.py` feeds `compute/analysis/lighting.py`, which feeds the store's read
  resolution and histogram, which feed `app.py`'s endpoints, which feed the UI. Each step
  needs the previous one's shape, so parallelism would buy guessing, not speed.
- All the admin-next work (Lighting card, `LIGHT` calendar cell, Frame review marker,
  All/Day/Night selector) lands in the **same single HTML file**, so it cannot be split off
  as an independent stream either.
- Opus rather than a cheaper tier because the two `(new)` decisions need interpreting, not
  transcribing: the `calibrated`-flag semantics, and the selector scoping scoring while
  leaving the re-run's frame set alone.

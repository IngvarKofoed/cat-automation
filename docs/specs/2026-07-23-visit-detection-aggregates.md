# Per-visit detection aggregates + a `corrupted` subject

A read-time extension of `Store.events()` that (1) adds a `corrupted` rung to the
event-subject ladder — for a motion visit where YOLO detected nothing *and*
corruption is present — and (2) attaches three YOLO-detection aggregates to each
event: max confidence, mean confidence, and detection ratio over the visit's
cat/person/bird frames. Both are derived at read time from the already-stored
`yolo-serial` and `corruption` verdicts; no new storage. This is the **data layer**
for a future evaluation feature (measuring YOLO's per-visit recall/confidence on
our real scene) and for auto-deprioritising glitch-explained visits — the UI that
*shows* these values is out of scope here.

## Key decisions

- **`corrupted` inserted into the subject ladder** (extends). `Store._classify_subject`
  (`store.py:2646`) gains a rung between `bird` and `unrecognized`: when no
  cat/person/bird box cleared `_ANNOTATE_MIN_CONF` (0.3) *and* any frame in the visit
  carries a `corruption` verdict → `{kind: "corrupted"}`; otherwise the ladder is
  unchanged. Because it
  only fires when YOLO detected **nothing**, a cat in a corrupt frame still resolves to
  `cat` — the fail-safe from the corruption decision is preserved.
- **`corrupted` is deprioritised, not hidden** (new). It marks glitch-explained motion
  as low-interest so `unrecognized` sharpens to "cat-scale motion YOLO couldn't name
  and that isn't a glitch." But corruption *can* mask a real cat, so a `corrupted`
  visit must stay reachable (it still appears in the feed / corruption page) — the
  taxonomy never asserts "definitely nothing."
- **Corruption presence via a new lookup in `events()`** (extends). `events()` already
  fetches the span's `yolo-serial` rows once and slices them per event by `bisect`;
  add a parallel fetch of the span's `corruption` verdicts (`_CORRUPTION_ANALYZER`) and,
  per event, decide "corruption present" from its frames. Mirrors the existing
  `subj_rows` pattern; no schema change.
- **Three aggregates from the same box-parse, attached to the event** (extends). In the
  same per-event loop, reuse `_subject_classes`/`_detail_boxes`/`_box_class` to compute,
  over the visit's cat/person/bird detections: `conf_max`, `conf_mean`, `ratio`. Attach
  as an event field (e.g. `event["detection"]`). Computed on read, **not persisted** —
  visits are a derived `_gap_split` projection whose bounds shift until settled and whose
  frames age out under eviction, so a stored per-visit row would go stale/orphaned; the
  per-frame boxes are already durable in `analysis`.
- **Confidence comes from `detail.boxes`, not the top-level `score`** (reuses). The
  `analysis.score` column is CAT-only max confidence; person/bird confidences live only
  in `detail["boxes"]` as `[x1,y1,x2,y2,conf,cls]`. The aggregates read per-class conf
  from there (via `_subject_classes`), so person/bird visits get real numbers too.
- **Scoped to `events()` (Activity motion-cluster visits), not the annotation inbox**
  (diverges). The annotation inbox clusters `yolo-serial`-*present* frames, a different
  clustering than `events()`' motion clusters — so a "shared helper for both" isn't a
  clean fit and isn't needed. Subject + identity already live in `events()`; the
  aggregates + `corrupted` join them there.
- **Graceful coverage fallback** (reuses). Both are read-time derivations: with no
  `corruption` verdicts over a span, `corrupted` simply can't fire and those visits stay
  `unrecognized`; with no `yolo-serial` rows the aggregates are the empty case. Same
  "an un-swept span still gets an honest label" property the ladder already has.

## Goals

- Give every Activity visit a `corrupted` subject where YOLO saw nothing and the motion
  was a glitch — so the review naturally deprioritises them and `unrecognized` becomes
  the genuinely-worth-a-look bucket.
- Record per-visit `conf_max` / `conf_mean` / `ratio` so a future feature can measure,
  on our real door: YOLO's per-visit detection rate (recall proxy) and where real-cat
  confidence sits vs phantoms (to set an operating threshold).
- Add no storage and no new sweep — pure read-time aggregation over existing verdicts.

## Non-goals

- The UI that displays/sorts/filters by these values — a future feature.
- The human-review evaluation workflow (labelling visits to turn `ratio`/confidence into
  measured YOLO recall/precision + a threshold recommendation) — the feature these feed.
- The one-time MOG2-recall confirmation (YOLO over non-motion frames) — adjacent, separate.
- Any persisted visits table / schema change; any change to the annotation inbox.
- Changing YOLO, the motion gate, or the corruption detector.

## Design

### The `corrupted` rung

`_classify_subject` takes a new `corruption_present: bool` and inserts one rung:

```
1. cat            (a cat box ≥ 0.3, or a confident gallery name — promotion in events())
2. person / bird  (box ≥ 0.3)
3. corrupted      ← NEW: no cat/person/bird box ≥ 0.3 AND corruption_present
4. unrecognized   (peak_area ≥ floor.min_area OR n_frames ≥ floor.min_frames)
5. motion_only
```

`events()` computes `corruption_present` per event as **True if ANY frame in the
visit's id span `[start_id, end_id]` carries a `corruption` verdict** — motion or not
(a glitch anywhere in the visit's window counts; deliberately NOT motion-filtered). The
visit already has no detection, so a single glitch is enough to file it as
glitch-explained rather than genuinely `unrecognized`. Aggressive on purpose — and safe
because `corrupted` is deprioritised-not-hidden: a
missed cat that happens to share a frame with a glitch is still reachable in the feed /
corruption page.

### The aggregates

In the per-event loop, over the visit's frames, using the per-frame max confidence
among classes {cat 15, person 0, bird 14} (from `_subject_classes`):

- `ratio`    — (visit motion frames with ≥1 such box) / the visit's motion-frame count
               (`n_frames`), both over the visit's MOTION frames (thread the per-frame
               motion flag). It DISTINGUISHES coverage: `null` when NONE of the visit's
               motion frames were swept ("not measured"), vs `0.0` when swept but nothing
               detected (a real YOLO miss the future recall stat must see). A partially-
               swept visit deflates `ratio` (accepted edge case — the live worker sweeps
               whole visits).
- `conf_max` — max of those per-frame confidences; `null` if the visit has none.
- `conf_mean`— mean of those per-frame confidences; `null` if the visit has none.

Attached as `event["detection"] = {"ratio": …, "conf_max": …, "conf_mean": …}`. A
not-measured (un-swept) visit → `{ratio: null, conf_max: null, conf_mean: null}`; a swept
miss → `{ratio: 0.0, conf_max: null, conf_mean: null}`.

Aggregates are recorded over **all** detections regardless of confidence (no 0.3 floor),
so the future feature can study the full confidence distribution, including the
sub-threshold detections the subject ladder ignores. (The subject ladder keeps its own
0.3 floor; the two are independent.)

### Combined vs per-class

Per your call, the aggregates combine cat/person/bird into one detection-density signal
(a visit is almost always a single subject). If per-class recall is wanted later (e.g.
YOLO's *cat*-specific hit-rate), we'd additionally stamp the dominant class + its
class-specific ratio — noted, not built.

## Alternatives considered

- **A shared visit-aggregation helper reused by `events()` and the annotation inbox.**
  Rejected: they cluster on different signals (motion vs detection), so it's not one
  computation; the aggregates belong on the motion-cluster (Activity) visits where subject
  already lives.
- **Persist the aggregates / a visits table.** Rejected: visits are derived and mutable
  (shifting bounds, eviction); the raw per-frame boxes are already durable, so read-time
  aggregation can't go stale.

# Missed-visit review on the admin-next tuning scorecards

Each scorecard on admin-next `#tuning` (Live gate / Baseline / Candidate) gains a
**Show missed (N)** toggle that opens a full-width panel below the three cards,
listing that column's *wholly-missed* visits — the visits whose gate never fired
— each as an inline filmstrip of its frames with YOLO boxes drawn. This is
admin-next's replacement for the old `/admin` visit inbox, and it closes the
redesign spec's deferred *"a miss count links to those frames"* item.

The listed visits come from `gate_scorecard` itself rather than from a second
query, so the list is the same set of spans the headline `wholly_missed` count is
computed from — not a lookalike computed by a different rule.

## Key decisions

- **`gate_scorecard` emits the spans it already clusters** (extends). It reads
  `interesting` rows and clusters them into visits purely to *count* them, then
  discards the spans. A new opt-in `missed_visits: bool` makes it also return the
  wholly-missed spans as records. Absent the flag, the returned card is
  byte-for-byte unchanged — the additive contract `oracle_floor`, `is_night`, and
  `flags=1` all follow.
- **The list is uncapped** (new). It is always exactly the `wholly_missed` set, so
  a reader never has to wonder whether they are looking at a sample. Affordable
  because each record is seven scalars (~200 bytes) and the *frames* — the
  expensive part — are fetched lazily per row, so a 300-miss day costs ~60 KB of
  JSON and zero images until something scrolls into view.
- **Not `Store.visits` / `/api/visits`** (diverges). The existing missed-visit
  inbox judges "caught" against the **live gate only**, drops no warm-up prefix,
  and ignores `oracle_floor`. Reusing it would give the Baseline and Candidate
  columns *the live gate's misses* under their own heading, with a length that
  disagrees with the number above it. `/api/visits` stays as-is for old `/admin`.
- **Consistency is structural, not maintained** (reuses). The records are built
  from the same `_split_into_visits` / `_visit_caught` pass that produces
  `visits.wholly_missed`, so `len(missed_visits) == wholly_missed` cannot drift.
  This is why the list is not a separate endpoint: the warm-up
  resolution lives in the *compare route* (`app.py:1723-1730`), so a second
  endpoint would have to duplicate it or factor it out, and a miss list computed
  over a different warm-up prefix than its own scorecard is precisely the class of
  bug entries 22/24 exist to prevent.
- **`interesting` grows two columns** (breaking, internal). Rows become
  `(recv_ts, motion, present, frame_id, oracle_score)`. `_cluster_visits` and
  `_cluster_visits_split` currently tuple-unpack three values and must switch to
  positional indexing. Both are private classmethods with no external callers.
- **`/api/tuning/compare?missed=1`** (extends). One query param threads the flag
  into all three `gate_scorecard` calls. Default `false`, so the response is
  byte-identical for any existing caller.
- **Filmstrips load lazily, per visit** (new). Each visit row renders its header
  immediately; its frames are fetched only when the row scrolls into view
  (`IntersectionObserver`). This is what makes the uncapped list affordable — a
  day with 40 misses would otherwise fire 40 `/api/frames/sample` requests the
  instant the panel opens.
- **Reuses the shared tile chrome** (reuses). `.ftile` / `.fimg` / `.fbox` /
  `.boxcap`, `placeBoxes()`, and `bandOf()` — the same pieces Frame review and
  Activity playback render with, so a missed frame looks identical wherever it
  appears.
- **Chronological order, not `visits()`'s worst-first** (diverges). One column is
  open at a time, so a stable time order turns toggling between columns into a
  visual diff. Worst-first would reorder every row per column.
- **Carries the motion-only caveat** (reuses). Compare returns
  `motion_only_spans` alongside the misses, as `/api/visits` and `/api/timeline`
  already do, so an unmeasurable window is never rendered as clean recall.

## Goals

- See *what* a gate configuration missed, not just how many, without leaving the
  tuning page.
- Compare the three columns' misses against each other — the point of tuning is
  "did my candidate params recover the visits the live gate dropped?"
- Keep the panel's contents provably the same visits the scorecard counted.

## Non-goals

- **False triggers.** The counterpart panel for `false_triggers.count` is not in
  this pass. The structure generalizes, but visit recall is the metric being
  tuned (entry 46) and one panel is enough to prove the shape.
- **`conflict` mode.** The old inbox's YOLO-vs-BSUV conflict view has no home
  here; it belongs to oracle selection, not gate tuning.
- **Labelling or acting on a visit.** This is a read-only review surface.
  Annotation is page 4's job.
- **Replacing `/api/visits`.** Old `/admin` keeps working untouched.

## Design

### Backend — `Store.gate_scorecard`

The `interesting` SELECT (`store.py:2619`) gains two columns:

```sql
SELECT f.recv_ts, {src_motion}, CASE WHEN {present_core} THEN 1 ELSE 0 END,
       f.id, o.score
```

`_cluster_visits` and `_cluster_visits_split` switch from `for ts, motion, verdict
in interesting` to positional reads (`r[0]`, `r[1]`, `r[2]`), leaving their logic
and outputs identical.

A new keyword-only `missed_visits: bool = False` adds one more pass over the same
in-memory rows when set. It reuses `_split_into_visits` on the present timestamps,
but carries the present *rows* alongside so each span can name its frames — then
keeps only the spans `_visit_caught` says were **not** caught:

```python
"missed_visits": [
    {"start_id", "end_id", "start_ts", "end_ts",
     "n_present", "rep_frame_id", "peak_score", "night"},
    ...
]
```

- `start_id` / `end_id` are the min/max frame ids of the visit's present frames —
  load-bearing, since the filmstrip fetches `/api/frames/sample?since_id=&until_id=`.
- `rep_frame_id` is the highest-oracle-score present frame (ties by id), matching
  `visits()`'s `missed` representative rule — including its NULL handling: a row
  with no stored score sorts as `-inf`, so it never wins the pick over a scored
  peer. It is what the row shows *before* its filmstrip loads (below).
- `peak_score` is that frame's oracle score — `null` when the whole span is
  unscored; `n_present` the present-frame count.
- `night` is present only when `is_night` was supplied, bucketed by the span's
  first present frame exactly as `_cluster_visits_split` does.
- Ordering is **chronological** (`start_ts` asc), deliberately *not* `visits()`'s
  worst-first rule. Only one column's panel is open at a time, so a stable
  time order makes toggling Live gate → Candidate a visual diff: rows hold their
  place and a recovered visit simply disappears. Worst-first would reshuffle every
  row between columns and defeat the comparison the page exists for.
- Uncapped: the list length is always `visits.wholly_missed` exactly. That
  equality is the point of the design, so nothing is allowed to truncate it.
- The `needs_rerun` short-circuit carries no `missed_visits` key, like `split`.

### Backend — the compare route

`/api/tuning/compare` gains `missed: bool = Query(default=False)`, passed to all
three `gate_scorecard` calls. When set, the response also carries
`motion_only_spans` for the scoped window — the same `store.motion_only_spans`
call `/api/visits` and `/api/timeline` already make, and for the same reason (see
below). It rides the flag rather than being unconditional so the default response
stays byte-identical.

### Frontend — the expander

In `mountTuning`, each `scorecardHtml` card gains a footer button:

> `Show missed (12)`

— rendered only when `sc.visits.wholly_missed > 0` and the card carries a
`missed_visits` array. Clicking toggles the shared full-width panel in a new card
below `#scorecards`, headed with which column it belongs to (*"Live gate — 12
missed visits"*) and the day already in scope. Only one column is open at a time;
clicking the open column's button closes it. `Compare` collapses the panel, since
its contents belong to the previous response.

Each visit is a row:

- **Header line** — clock time, duration, `N frames`, peak YOLO confidence, and
  the Day/Night tag when the split is on.
- **Filmstrip** — an `.fgrid` of `.ftile`s for the visit's frames, from
  `/api/frames/sample?since_id=&until_id=&detections=yolo-serial&count=24`, each
  tile drawing its YOLO box via `placeBoxes()` banded by `bandOf(score)`. A frame
  with no box gets no overlay; the existing `analysed` vs `no detection`
  distinction (entry 113) is preserved by the shared chip renderer.

Until the strip loads, the row shows `rep_frame_id` as a single placeholder tile,
so an unscrolled panel is a readable list of thumbnails rather than a stack of
empty headers.

`count=24` decimates a longer visit **by frame index**, and the response cannot
say whether it did. So the row labels its strip `N of M frames` from `n_present`,
never implying the whole visit is on screen — the same honesty entry 154 had to
retrofit onto Frame review.

The strip covers the visit's own `[start_id, end_id]` span only — no surrounding
context. Frame review is the tool for "what was happening either side of this",
and it takes the same id bounds.

The strip is fetched on first intersection and cached on the row, so scrolling
back up re-renders from memory. A failed fetch renders a per-row error and leaves
the rest of the panel working.

### The unmeasurable-misses warning

Under motion-only capture the non-motion frames a miss *lives in* were never
stored, so a miss inside such a span cannot be observed at all — and a short or
empty list then reads as good recall when it is really an absence of evidence.
This panel is the sharpest instance of the trap entries 97 and 126 already
records: an empty danger set must never render as "safe".

So when the scoped window overlaps any `motion_only_spans`, the panel opens with a
banner naming the overlapping stretch and saying misses are **unmeasurable**
there. It shows on an empty list too — that is the case it exists for.

### Why this reads well against the rest of the page

The panel answers the question the three scorecards raise and cannot answer:
*"Candidate recovered 4 visits — which ones, and were they real cats?"* Because
every column's misses are drawn from its own source verdicts, opening Live gate
and then Candidate over the same day shows exactly which rows disappeared.

## Alternatives considered

- **A lazy `/api/tuning/missed` endpoint.** Leaner compare payload, paid only on
  expand. Rejected because the warm-up prefix and the per-slot area thresholds are
  resolved in the compare *route*, so a second endpoint duplicates that resolution
  — and a miss list warmed differently from its own scorecard is a silent lie.
- **Reuse `/api/visits?mode=missed` and deep-link to Frame review** (the redesign
  spec's original plan). Cheapest, but it is live-gate-only, so Baseline and
  Candidate get no list at all, and its count disagrees with the headline.
- **Expanding inside each `.stat` card.** Keeps the list welded to its column, but
  the three-up grid squeezes filmstrip tiles to postage stamps.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Three files on one dependency chain —
  `store.py` (`interesting` columns, the two `_cluster_visits*` readers, the new
  records), `app.py` (one query param), then the `mountTuning` panel that consumes
  the shape the first two produce. The frontend can't be written until the payload
  is settled, so parallelism would only produce guesswork.
- Worth a `test_scorecard.py` case asserting `len(missed_visits) ==
  visits.wholly_missed` — that equality is the whole reason for this design and is
  cheap to pin.

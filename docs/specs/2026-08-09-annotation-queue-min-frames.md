# A minimum-frames filter on the annotation queue

The annotation queue's membership rule is **undecided** — no `dataset_items` row — not
*uncertain*, so every visit the door produces waits there until a human decides it. That
is correct, and it is also why single-frame visits accumulate: a visit like
`1 frames · rep 52% · peak 52%` yields one crop, that crop will not be gallery-grade, and
a gallery-only build and a gallery-only validation run both ignore it entirely. It costs
operator attention and returns nothing. This adds a **frame-count floor** to the queue —
server-side, before the page cap — so the daily working set holds visits that can actually
contribute.

The only existing filter, `uncertain_only` ("Hide confident matches"), keys on the *model's*
verdict. This one keys on the *visit's* substance; the two are independent and compose.

## Key decisions

- **Frame count, not gallery-grade presence** (new). `min_frames` drops visits with
  `len(v["frames"]) < min_frames`. The better predicate is "has at least one frame at
  `score >= 0.6 && area >= 0.7 × peak_area`" — exactly what a gallery-only build enrols —
  but computing it server-side means a second copy of `seedQuality`
  (`index.html:2335`) in Python, and that formula has a known defect: a one-frame visit's
  `ratio` is `bboxArea(f) / v.peak_area` where the frame *is* the peak, so it is 1.0 by
  construction and the area test cannot fail. Encoding that server-side would bake the bug
  into the queue and make fixing it a two-place lockstep change. Frame count is orthogonal
  to it.
- **Both predicates are evaluated together, replacing the `uncertain_only` block** (extends).
  The rewritten block sits where that one does (`store.py:5507`) — after the sort, before the
  `limit` truncation, for the reason it documents: applied after the cap, hidden visits would
  still eat the page's 100 slots and the filter would appear to do nothing on a busy store.
  Not two sequential filters, because sequencing makes the counts depend on their order.
- **Each hidden count is "what relaxing THIS control reveals"** (new). `hidden_confident` is
  measured with the frame floor *still applied* (confident **and** thick), and `hidden_thin`
  with the confidence filter still applied (thin **and** uncertain). So unticking either
  control reveals exactly the number the page quoted. The alternative — counting each filter's
  full catch independently — makes `hidden_confident` include confident-*and*-thin visits that
  unticking would not reveal, i.e. a promise the page cannot keep, in the one readout that
  exists to stop a filtered queue reading as a finished one.
- **A visit failing BOTH predicates is counted in neither** (new). It is not revealed by
  relaxing either control alone, so no single-control number can honestly claim it. The
  consequence: the two counts do not sum to the number hidden, and the composed status line
  must not be phrased as though they do.
- **A third field, `hidden_total`, carries the completion signal** (new). Measured
  (pre-filter minus post-filter), never summed from the two — summing is exactly what the
  bullet above forbids. It exists because gating "nothing left" on the per-control pair
  declares the queue clear whenever *every* remaining visit fails both predicates, which the
  default-on floor plus "hide confident matches" reaches routinely. The celebration gates on
  this; the per-control counts only supply the text naming what to relax.
- **Reports `hidden_thin` at all** (reuses). Mirrors `hidden_confident`'s existence. An
  unreported filter turns a short queue into what reads as an empty one — entries
  97/126/167/304's recurring trap, and the specific reason `hidden_confident` exists.
- **`min_frames=1` is the API's no-op default; the UI sends 2** (reuses). Absent the
  parameter the endpoint answers byte-identically to today, plus `hidden_thin: 0` — the same
  additive discipline `uncertain_only` was added under. The default-on behaviour lives
  entirely in the client, so no other caller of the endpoint changes.
- **UI ships a checkbox fixed at 2, CHECKED by default** (diverges). The control is "Hide
  single-frame visits" beside `#aUncertainWrap` (`index.html:2266`). It diverges from
  `uncertain_only`, which ships off: single-frame visits are the everyday case here, so the
  useful state is the default rather than a habit to remember. The cost is that the queue
  omits visits from first load, which makes `hidden_thin` **load-bearing rather than
  informational** — it is the only thing on the page telling an operator that a shorter queue
  is a filtered one. A number input was rejected: it would add a third focusable control to a
  keyboard-first page, where focus left in an input silently swallows the next label (entries
  235/242/298). The parameter stays a general integer, so a number input is a later UI change,
  not an API change.
- **Queue mode only; Flagged is deliberately untouched** (reuses). The filter lives on
  `annotation_queue_page`, which serves Queue alone. Flagged spans are already unfloored on
  purpose (entry 225: a human pointing at one visit is not the bulk case), and a thin flagged
  visit is often exactly what was flagged.
- **Not persisted** (reuses). Held in a module-level `let`, as `uncertainOnly` is — so a
  reload returns to the *default*, which here means the filter comes back **on**. An operator
  who unticked it to work the thin tail gets it back next session; that is deliberate, since
  the filtered view is the intended working state and the untick is a one-off excursion.

## Goals

- Keep single-frame visits out of the daily working set without labelling them.
- Never let a filtered queue read as a finished one.
- Compose cleanly with `uncertain_only` rather than replacing or subsuming it.

## Non-goals

- **Changing queue membership.** Hidden visits stay undecided and reappear the moment the
  filter comes off. This shortens the *page*, not the backlog — `ignored` remains the only
  permanent disposal.
- **Fixing `seedQuality`'s ratio=1.0 defect.** Real, and a separate change.
- **Filtering the unpaginated `/api/label/visits`** (`label_queue`, `app.py:2390`). It was
  considered so an outstanding-work readout could not disagree with the filtered page, but
  that readout no longer exists — the endpoint has no consumer in `admin-next` or the user
  app (only `test_annotation.py` and `test_annotation_p4.py` call it), the progress display it
  was kept for having gone with the old console in M6. Its `label_progress` half also counts
  `total_visits` over *all* present frames **including decided ones**, so a frame floor there
  would filter the denominator of a progress ratio rather than a work queue. Left unchanged;
  if the readout returns, the floor can be added then, against a consumer whose semantics are
  known.
- **Any change to what the collector stores or the detection worker analyses.**

## Design

### Store

`annotation_queue_page` (`compute/collection/store.py:5345`) gains a keyword-only
`min_frames: int = 1`, floored at 1 (`max(1, int(min_frames))`) so a zero or negative value
degrades to the no-op rather than raising. Note this is *not* the kind of clamp `limit` gets:
no upper bound protects against an absurd value emptying the queue, because any floor above
the largest visit empties it legitimately. What keeps that from being silent is `hidden_thin`
— the empty stage reports the count and names the control. Immediately after the
`uncertain_only` block at `store.py:5507`:

```python
# Each visit is tested against both predicates once. The counts are then measured
# with the OTHER filter still applied, so each answers "how many more would I see
# if I relaxed THIS control" — the question an operator toggling one checkbox asks.
drop_confident = uncertain_only and model is not None
keep_conf = lambda v: not drop_confident or v["uncertain"]
keep_thick = lambda v: len(v["frames"]) >= min_frames

hidden_confident = sum(1 for v in visits if keep_thick(v) and not keep_conf(v))
hidden_thin = sum(1 for v in visits if keep_conf(v) and not keep_thick(v))
visits = [v for v in visits if keep_conf(v) and keep_thick(v)]
```

This replaces the existing `uncertain_only` block rather than following it — sequencing two
filters would make each count depend on which ran first. With `min_frames = 1` (the API
default) `keep_thick` is universally true, so `hidden_confident` and the surviving set are
byte-identical to today's; the rewrite changes no existing caller's answer.

`hidden_thin` joins the returned dict beside `hidden_confident`. Note that a visit failing
**both** predicates — confident *and* thin — is counted in neither, because relaxing either
control alone would not reveal it. The two counts therefore do not sum to the number hidden,
which is a property the readout wording has to respect (see *Frontend*).

Note the frame count here is **boxed frames above `_ANNOTATE_MIN_CONF`** (the queue's
universe, `store.py:5426`), not every frame in the span. A visit whose span holds forty
frames of which one carries a box counts as one, which is the intended reading — one crop is
what a label would produce.

### API

`/api/label/queue` (`compute/api/app.py:2392`) gains `min_frames: int = Query(default=1)` and
passes it through. No validation beyond the store's clamp.

### Frontend

`loadQueue` (`index.html:2691`) appends the parameter, so the query string becomes a built
list rather than the current single-flag ternary at `index.html:2698`. `minFrames` is a
module-level `let` beside `uncertainOnly` (`index.html:2300`) initialised to **2**, with
`hiddenThin` beside `hiddenConfident`.

Because the filter is on from first load, the empty stage stops being an edge case: a store
holding only thin visits shows it immediately, to an operator who has toggled nothing. That
path must name the count and the control to relax — the existing empty state already does
this for `uncertain_only` (`index.html:2443-2446`), and it is the reason the composed message
below is worth doing properly rather than appending a second sentence.

The checkbox mirrors `#aUncertain`'s listener exactly, including the `e.target.blur()` and
the re-fetch (the filter runs before the server's cap, so a reload is what actually fills the
page). It needs no `renderUncertainToggle`-style enable/disable — unlike confidence, frame
count is meaningful with no promoted model — but it is hidden outside Queue mode the same way.

**The status and empty-state lines are where the care goes.** Today two sites hardcode a
single hidden reason (`index.html:2412-2416`, `2443-2446`); with two filters the combinations
multiply. Both call sites should instead read one helper that composes the fragments from
whichever counts are non-zero — `· 12 confident, 34 thin hidden` — so the status line and the
empty stage can never describe the same queue differently. The empty state keeps its existing
job of naming which control to relax, now for either filter or both.

The wording must attach each number to **its own control** ("12 confident hidden", "34 thin
hidden") and never present a total. Per the store's counting rule a visit failing both
predicates is in neither figure, so any phrasing implying the two account for everything
hidden — "46 hidden in total", or an empty stage saying "untick both to see all 46" — would be
false. Naming them separately is not merely tidier here; it is the only honest form.

One edge case, deliberately left alone: `loadQueue` drops locally-`decided` frames *per
frame*, not per visit (`index.html:2708-2713`), so a server-approved visit can arrive with
three frames and render with one while its write settles. Re-applying the floor client-side
would add a second, drifting copy of the rule to suppress a visit that is about to vanish
anyway on the next load.

### Test

`compute/tests/test_annotation_p4.py` holds the queue's existing coverage. The cases worth
pinning:

- The floor drops thin visits and reports the count.
- `min_frames=1` returns exactly what omitting it does, `hidden_confident` included — the
  regression guard on rewriting the `uncertain_only` block.
- **The counting rule, with both filters on and all four combinations present**: untick either
  control and exactly the quoted number of visits appears. This is the assertion that fails if
  someone re-sequences the filters, and it cannot be caught by inspection — both orderings
  produce the same surviving set and differ only in the counts.
- A confident-and-thin visit is in neither count.
- The filter runs **before** the cap — a store with 100+ thick visits and a pile of thin ones
  must return a full page of thick ones, which fails if the block moves below the truncation.

## Alternatives considered

- **Gallery-grade presence as the predicate.** The honest value test, and the server already
  holds every input (`score`, `bbox`, `peak_area`). Rejected for now only because it requires
  duplicating `seedQuality` into Python while that formula has a known structural defect. Worth
  revisiting *after* the seed is fixed — at which point it could replace the frame-count floor
  rather than sit beside it.
- **Sort thin visits last instead of hiding them.** Nothing is hidden, so nothing can be missed.
  Rejected: it compounds with the worst-first distance ordering into an order that can't be
  reasoned about, and the 100-cap then truncates the thin tail silently — which makes
  `truncated` mean something different from what it means everywhere else.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Three files (`store.py`, `app.py`, `index.html`) plus
  `test_annotation_p4.py`, all on one code path — the store predicate, the parameter that
  reaches it, and the readout that reports it have to agree, so splitting them buys nothing.
  The counting rule is the part that needs judgment rather than transcription: both filter
  orderings yield the same visits and differ only in the two numbers, so a builder who does
  not hold the whole rule at once will write something that passes casual inspection and
  quietly lies to the operator. That is what keeps this off a cheaper tier.

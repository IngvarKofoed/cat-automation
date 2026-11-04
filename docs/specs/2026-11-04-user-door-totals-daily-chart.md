# Daily line charts for the "At the door" card

Each line of the "At the door" card on the user app's Visits page becomes tappable: Cat visits,
Our cats, Neighbours, Unknown cats, and All movement at the door. Tapping one opens a page with
a line chart of that figure per day over the last 30, 60 or 90 days (60 by default). This is
the household counterpart to the per-cat chart
(docs/specs/2026-11-04-user-cat-daily-visits-chart.md), and it reuses that chart's machinery
throughout:
- **The count.** The numbers come out of the same 90-day count, which is extended to keep the
  per-day figures behind the five lines beside its per-cat figures. One walk and one 10-minute cache serve every cat and
  every line, so these pages add no counting time.
- **The page.** The window toggle, day readout, day states and notes are the cat chart's.
- **The difference.** The chart is drawn as a line, not as bars.

## Outcome

**What you get:**

- Each of the five lines in "At the door" opens its own page. It shows a line of that figure per
  day over 30, 60 or 90 days, a daily average, and a readout for whichever day you tap.
- A day with no pictures, only part of a day's pictures, or no names yet is marked as such. It
  is never drawn as a dip. All movement still draws days the model hasn't named yet.
- There is no extra wait. Within the 10 minutes, a line opens instantly after any cat's chart,
  and the reverse holds too.

**How to verify:**

- Open Visits and tap Our cats. A 60-day line appears:
  - it is broken across 27–31 Oct, which read "not recorded";
  - 26 Oct and 1 Nov have hollow points that read "partly recorded — at least N";
  - today's point is hollow and reads "today so far".
- Tap 3 Nov on Our cats. The readout equals the sum of the four residents' own charts for that
  day. 3 Nov's figure, measured on 4 Nov, was 15 + 15 + 18 + 49 = 97. On Cat visits, the same
  day equals Our cats + Neighbours + Unknown cats.
- Open a cat's chart. Then, within 10 minutes, reload the app on a line's own address, for
  example `#visits/door/all-movement`, with the browser's network panel open.
  `/api/door-stats/daily` answers in well under a second (0.27 s warm, against 6.1 s cold). The
  reload is what makes this a real check: without one, the page redraws from the data it already
  holds.
- Open Neighbours. A note says Store Kali isn't in the current model.
  - After promoting a newly built gallery, the four named lines read "not named yet" on days the
    Identify all pass hasn't reached.
  - All movement still draws those days.

## Key decisions

- **Same count, same cache** (extends `daily_visit_counts`). The walk that counts each cat per
  day also counts the figures behind the five lines. Opening a line after a cat's chart, or the reverse, is
  served from the one cached count. Measured on the compute PC today, the shared count costs
  6.1 s cold and 0.27 s warm.
- **"Cat visits" is drawn as Our cats + Neighbours + Unknown cats** (diverges from the card's
  `cat_visits`).
  - The card's figure also counts *unidentified* visits: a cat was detected, but nothing has
    named it yet. That is the figure the card's footnote quotes.
  - Counting those needs the event subject, which roughly doubles the walk. Measured at 0.45 s
    per 500 visits by identity alone and 0.96 s with the subject.
  - In the newest 500 visits there were none, so the two agree except while naming lags, chiefly
    after a promotion.
  - With no usable gallery, the card counts every cat visit as unidentified. The Cat visits page
    shows the no-gallery explanation instead of a chart.
  - The client sums the three; no per-day `cat_visits` field is sent. A field named like
    `/api/door-stats`' `cat_visits` but meaning something else would break entry 348's rule.
  - The other four lines use the card's exact definitions.
- **All movement ignores naming** (new). It counts every event the feed produces, named or not,
  so a day not yet named still has a movement figure. Each day therefore carries a second state
  without `unnamed`.
  - The walk now also runs when there is no usable gallery, so All movement never waits on a
    model. That diverges from the per-cat spec, which skipped the walk without one.
  - Accepted consequence: with no usable gallery, the cat chart's first open now shows
    "Counting" for ~6 s before its explanation, instead of showing it at once. That state
    occurs only before a first calibrated model.
- **Line chart, on these pages only** (new). The cat chart keeps its bars.
  - A line breaks at every day with no figure, and is never drawn across one.
  - Partly recorded days and "today so far" get hollow points and dashed segments. A floor, or
    an unfinished day, must not read as a dip.
- **Route `#visits/door/<line>`** (extends the router). The card's five rows become links with a
  chevron, like the cat rows below them, and the Visits tab stays highlighted.
- **The cat chart's page, reused** (extends). The following carry over unchanged:
  - the load with its sequence guard;
  - the 30 / 60 / 90 toggle as a client-side slice;
  - the summary, day selection, readout and hidden table;
  - no live refresh.
  What differs: the header, the chart drawing, the notes' wording (they describe hollow points
  and dashed lines, not lighter bars), and "movements" in place of "visits" on All movement.
- **Says who isn't in the model** (reuses `enrolled_cat_ids`). The Our cats, Neighbours and
  Unknown cats pages name any active roster cat that the active model leaves out. Such a cat's
  visits are counted as an unknown cat or, usually, as whichever enrolled cat it most resembles.
  The model declines very few visits: 7 of the newest 500 read unknown. A reader of the
  Neighbours line would not otherwise guess either.

## Goals

- See each of the card's five figures per day over the last 30, 60 or 90 days, from its row.
- Never let an unrecorded, partly recorded or unnamed day read as a quiet one: the per-cat
  chart's rule, carried over.
- Add no counting time and no second count.

## Non-goals

- Changing the per-cat chart, including turning it into a line. The one side effect is its
  no-gallery wait, noted under Key decisions.
- Several lines on one chart, or a stacked breakdown of Cat visits.
- A day/night split for these figures.
- Lines for wind, people, or unidentified visits. These need the subject; see Alternatives.
- Windows other than 30 / 60 / 90 days, or an hourly chart behind the 6 h / 24 h columns.

## Design

### Data: per-day totals in `Store.daily_visit_counts`

**Counting.** The walk already classifies each event by its `identity`. It also folds the
identity half of `_classify_stats_event` into a per-day `totals` record. `door_events` is the
walk's existing per-day `events` count, sent as its own field beside `totals`:

| field | counts | same as the card's |
|---|---|---|
| `totals.resident` | named, `identity.is_resident` true | Our cats |
| `totals.neighbour` | named, not resident | Neighbours |
| `totals.unknown_cat` | `identity` present, `cat_id` null | Unknown cats |
| `door_events` | every event starting that day | All movement at the door |

Cat visits is the client's sum of the three `totals` fields; see Key decisions.

- **Which day.** Day assignment, the floor and truncation are exactly the cat chart's.
- **No gallery.** The early return without a usable gallery is removed, so the walk runs
  regardless. With no model, or an uncalibrated one, it still counts `door_events`, and the cat
  and named-total fields stay unused (see below).
- **Cache.** The cache key (local date, model id, threshold) already covers the no-model case.

**Merging.** `merge_daily` adds three per-day fields beside the unchanged `state` and `cats`:
`movement_state`, `totals` and `door_events`.

`movement_state` follows the `state` ladder with `unnamed` removed:

```
expired > none > uncounted > partial > full
```

On a day with no usable gallery, `state` still reads `unnamed`.

Today `merge_daily` reads a day's counts only when the gallery is usable. `movement_state` and
`door_events` must read them regardless; only `cats` and `totals` keep that gate. Kept for all
three, the gate would leave no counts on any no-gallery day. The zero rule would then draw All
movement as a flat zero, in exactly the case it is meant for.

Each day's response record becomes:

```
{date, start_ts, end_ts, today,
 state,                 // unchanged: the cat chart and the four named lines read it
 movement_state,        // new: All movement reads it
 cats,                  // unchanged
 totals: {resident, neighbour, unknown_cat} | null,
 door_events: int | null}
```

- **`totals`** is null unless `state` is `full` or `partial`.
- **`door_events`** is null unless `movement_state` is `full` or `partial`.
- **Zeros.** On a counted day with no such visits, each field is 0, never missing. Null means no
  figure, and a no-figure day is never drawn as 0.
- **API.** `GET /api/door-stats/daily` is otherwise unchanged.

### Page: `#visits/door/<line>`

**Getting there.**
- On `#visits`, the card's five data rows become links with a trailing chevron. The header row
  and the footnote stay plain.
- The slugs are `cat-visits`, `our-cats`, `neighbours`, `unknown-cats` and `all-movement`.
- `#visits/<digits>` stays the cat chart. An unknown slug shows "No such chart" with a link back
  to Visits.

**Header.** "‹ Visits", then the row's own label as the title, then a one-line definition:

| line | definition |
|---|---|
| Cat visits | Every cat through the door: ours, neighbours, and cats we didn't recognise. |
| Our cats | Visits by our own cats, all together. |
| Neighbours | Visits by named cats that live elsewhere. |
| Unknown cats | Cats the model looked at but couldn't match to any cat it knows. |
| All movement | Everything that set the camera off: cats, people, wind and shadows. |

There is no avatar. The page fetches `/api/cats/overview` alongside the counts, as the cat chart
does, for the model note below.

**Reused as is from the cat chart:**
- loading text and sequence guard;
- the 30 / 60 / 90 toggle (shared with the cat chart while the app is open);
- the truncation banner rule;
- the summary (average over `full` days excluding today, busiest day, "as of");
- column hit-testing with tap and drag, ← / → on the focused chart, the `aria-live` readout and
  the hidden table;
- no reload on SSE or poll.

Every word "visits" reads "movements" on All movement, for example "About 140 movements a day".
All movement reads `movement_state` wherever the others read `state`.

**No usable gallery.** The four named lines show the Visits page's own explanation
(`fillNoGallery`) in place of the chart. All movement draws as normal.

**Model note.** Some active roster cats can be missing from `enrolled_cat_ids`. When they are,
some pages carry one line naming them:
- Our cats names the residents it is missing.
- Neighbours names the neighbours it is missing.
- Unknown cats names both.

For example: "Store Kali isn't in the current model, so its visits are counted as an unknown
cat or as whichever cat it most resembles."
Nothing shows when `enrolled_cat_ids` is null, since an older model doesn't say.

**Older backend.** Days arriving without a `totals` key are detected by the key's presence, not
by its value. The page then says the compute PC may be on an older build, instead of drawing.
A frontend served against an older compute PC would otherwise draw every line as zero (entries
365/489's rule).

### Chart: one line per page

The frame matches the cat chart:
- the same plot height and axis band;
- a y-axis from 0 scaled to the window's highest figure, with ~3 gridlines;
- Monday date ticks, fortnightly at 90 days, and "today so far" at the right;
- no horizontal scroll at 360 px.

Drawn with the `dataviz` skill.

- **The line.** One stroke through each day with a figure, in one colour. Neighbours takes the
  neighbour colour a neighbour's own chart uses; the other four take the default. There are no
  per-day markers, since 90 points on a phone would be clutter.
- **Breaks.** A day with no figure ends the line, and the next figured day starts a new
  segment: a run of consecutive figured days. No-figure days get the cat chart's full-height
  muted band. A figured day with no figured neighbour on either side is drawn as a solid dot, so
  it isn't invisible.
- **Partly recorded and today.**
  - These points get a hollow marker. On an isolated day the hollow marker wins over the solid
    dot.
  - Dashing is per edge, not per segment: only the stretch between such a point and its
    immediate neighbours is dashed. The rest of the run stays solid. Dashing the whole run would
    dash nearly the entire line every day, because today ends it.
  - This is the line's form of the cat chart's lighter "at least" bar. Without it, a floor or an
    unfinished day reads as the figure dropping.
- **Selection.** The selected day shows a vertical rule spanning the plot and a filled marker
  on the line (none on a no-figure day). The readout wordings are the cat chart's, for example:
  - "Sat 25 Oct — 85 visits";
  - "partly recorded — at least 12";
  - "not recorded";
  - "not named by the current model yet". This one never appears on All movement.

### Docs

**ARCHITECTURE**, *Dashboard + API* row. Add after the per-cat chart sentence: each line of the
Visits page's "At the door" card drills down the same way. The page is a line chart of that
figure per day, served from the same cached count. Cat visits there leaves out not-yet-named
cat visits.

## Alternatives considered

- **Read the subject in the walk.** "Cat visits" would always match the card, and it would make
  wind, person or unidentified lines possible later. It roughly doubles the shared count, about
  6 s → 12 s cold, for both these charts and the cat chart, over a difference that is zero
  except while naming lags. Rejected by choice.
- **A separate count for these lines.** The cat chart would keep its speed, but the first open of
  each kind of page would wait for its own walk, and there would be two caches to keep
  consistent. Rejected.
- **Bars, like the cat chart.** This reuses the drawing as is. At 90 days on a phone each bar is
  ~3 px wide, and the trend these figures are read for shows better as a line. Rejected by
  choice. Also rejected by choice: turning the cat chart into a line as well.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** One thread across `store.py` (the count and the merge, with their
  tests) and `web/user/index.html` (links, router, page, line drawing). `app.py`'s route and
  cache need no change. The page reads the merge's new states directly, and the line's
  break, hollow-point and dashing rules are new drawing that has to be interpreted, not
  transcribed. The `dataviz` skill applies to the chart, `frontend-design` to the page.

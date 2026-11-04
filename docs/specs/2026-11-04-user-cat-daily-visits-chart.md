# Per-cat daily visits chart

Tapping a cat on the user app's Visits page (`#visits`) opens a page for that cat with a bar chart
of its visits per day over the last 30, 60 or 90 days (60 by default). The Visits page answers
"how much lately" over 6 h and 24 h. This page answers "is that normal for her": a trend, an off
day, or a seasonal shift. The server counts all cats over 90 days in one walk of the same event
feed the Visits page counts, using `door_stats`' paging. It keeps the result for 10 minutes, so
hopping between cats and windows is instant.
Every day carries a state, so a collection outage, or a stretch the current model hasn't named
yet, reads as such, never as a day the cat stayed home.

## Outcome

**What you get:**

- Tapping any cat on Visits opens its own page, with one bar per day over the last 30, 60 or 90
  days and a line saying about how many visits a day it makes.
- Days with no pictures, part of a day's pictures, or no names yet are marked as such, never
  drawn as a quiet day.
- After the first open, every other cat and every window opens instantly for 10 minutes.

**How to verify:**

- Open Visits and tap Saffi. A 60-day chart appears:
  - 27 Oct to 31 Oct read "not recorded";
  - 26 Oct and 1 Nov read "partly recorded — at least N";
  - 24 and 25 Sep, which straddle an overnight 11-hour gap, read partly recorded too.
- Tap yesterday's bar. The readout's count matches the number of Saffi visits Activity shows for
  yesterday.
- Tap another cat, then switch to 90 and to 30. Each appears with no wait, and a given day shows
  the same count in every window.
- Open Store Kali. The page says the current model leaves her out, instead of showing a row of
  zeros. After promoting a newly built gallery, days the Identify all pass hasn't reached yet read
  "not named yet", not zero.

## Key decisions

- **One walk over the feed, for all cats** (extends). New `Store.daily_visits` reuses
  `door_stats`' keyset paging over `events()` and its naming rule (a visit counts for a cat when
  its aggregated `identity` names that cat). So the chart, Visits, Cats and Activity can never
  name a visit differently (entry 341). It runs `with_subject=False`, the `cats_overview`
  precedent (entry 257), since a per-cat count reads only `identity`.
- **Kept 10 minutes in memory, not memoized on disk** (diverges from `calendar_days`, entry 381).
  The first open after a restart, or 10 minutes after the last count, pays an estimated ~8 s.
  Then every cat opens instantly. The cache is keyed on the active model and threshold, so a
  promotion or threshold move recounts at once. Chosen over a persisted per-day memo, whose exact
  invalidation (identify passes, eviction, Clear, threshold) took the calendar several review
  rounds.
- **Always 90 days on the wire; the window is a client-side slice** (reuses `door_stats`' "one
  read serves every window"). The toggle is a pure re-render: it never waits, and a day reads the
  same in every window. The trade is that a cold open costs ~8 s rather than the ~5–6 s a
  60-day-only count would.
- **Every day carries a state** (new). The states are `full` / `partial` / `none` / `expired` /
  `unnamed` / `uncounted`: recording read from stored frames, naming read from the walk itself.
  - On the real store, 22 of the last 60 days were wholly or partly unrecorded: 8 with no frames
    at all (09-10, 09-18, 10-07, 10-27–10-31), and 14 touched by a gap of 8–143 h.
  - Drawn as low bars, they would read as days the cat stayed home: entries 97/126/167/304's trap.
- **Days are the compute PC's local calendar days** (diverges from the tuning calendar's browser
  offset). The window spans a DST switch (2026-10-25), and one fixed offset would shift every
  earlier day by an hour. The PC is in the house it watches, and on Windows the OS clock is the only
  zone source without the `tzdata` package (see `compute/tools/ir_lamp_timeline.py`).
- **Route `#visits/<cat_id>`** (extends the router). The Visits tab stays highlighted, and each
  cat row on `#visits` becomes tappable. No new nav entry: this is a drill-down, not a destination.
- **No live refresh** (diverges from every other view). The SSE nudge and the fallback poll do
  not reload this page; route entry and app foreground resume do. A nudge arrives every few
  seconds while a cat lingers, and an expired cache would turn each nudge into a 90-day walk on
  the shared write connection. That is the entry 354 concern, multiplied.
- **Hand-built inline SVG, no chart library** (reuses). The user app is one dependency-free file.
  The chart is one bar per day, drawn with the `dataviz` skill as `compute/CLAUDE.md` requires.

## Goals

- See one cat's visits per day over the last 30, 60 or 90 days, from its row on the Visits page.
- Never let an unrecorded, partly recorded or unnamed day read as a quiet one.
- Opening a second cat, or switching window, costs nothing extra.

## Non-goals

- A household total per day, or several cats on one chart.
- A day/night split in the bars (decided: one colour; the split is in the tapped-day readout,
  and the Visits page already shows it).
- Windows other than 30 / 60 / 90 days.
- Linking a bar to that day in the Activity feed.
- Persisting the counts, or showing the chart in admin.
- Sparklines on the Visits page itself (the payload would support them later).
- Telling a quiet stretch from a collector outage exactly. The recording state is a heuristic
  over stored frames (see *Day states*), biased toward claiming less.

## Design

### Data: `Store.daily_visits`

`daily_visits(*, days=90, is_night=None, now_ms=None)` returns:

```
{generated_ts, window_days: 90, store_oldest_ts,
 model: {id, calibrated, enrolled_cat_ids: [..] | None} | None,
 truncated, split: {available, ...},
 days: [{date: "2026-11-03", start_ts, end_ts, today: bool,
         state: "full" | "partial" | "none" | "expired" | "unnamed" | "uncounted",
         cats: {"<cat_id>": {visits, day, night}}}]}
```

- **Days.** `days` holds exactly 90 entries, oldest first, and today is the last one. Each entry
  spans local midnight to midnight; `end_ts` is today's "now".
- **Sparse `cats`.** A day lists only cats with at least one visit. A missing cat reads as 0 only
  when `state` is `full` or `partial`.
- **Day/night.** `day`/`night` come from the injected `is_night` exactly as in `door_stats`.
  They are `None` when the split is unavailable.
- **Enrolled cats.** `enrolled_cat_ids` lists the cats in the active model's `metrics.per_cat`.
  It is `None` for a model built before that field existed.
- **Retired cats.** They are included. The client asks for one id, and the active gallery can
  still name a retired cat.

**Counting.** The walk pages `events(since_id, until_id, limit=_MAX_EVENTS, with_subject=False)`
newest to oldest, by keyset, exactly as `door_stats` does. Factor that loop into one shared helper
rather than copying it; it carries entry 350's floor rule and entry 271's empty-page rule. It takes
`with_subject` as a parameter, because `door_stats`' buckets need the subject and this does not.
- **Which day a visit belongs to.** A visit belongs to the local day of its `start_ts`, the rule
  `door_stats` uses, so a visit straddling midnight counts once, on the day it started.
- **The window's first day.** The floor is resolved from window start minus one hour, and visits
  starting before the window are discarded. Without that, a visit already under way at the floor
  would be clipped and land on the first day.
- **Page budget.** It gets its own `_MAX_DAILY_PAGES = 40` (20k visits). Measured: the last 60 days
  were ~6,500 visits, 13 pages, so 90 days is ~20 pages.
- **Per day, the walk also records whether any visit carried an `identity`** (named or unknown).
  That costs nothing extra, and it is what `unnamed` reads.

**Truncation.** If the budget runs out, `truncated` is set, and the day where counting stopped,
plus every older day, becomes `uncounted`. That includes the straddle day, whose partial count is
dropped rather than shown as recorded. The page then banners truncation only when its visible
window holds an `uncounted` day, so a 30-day view of a count that stopped 70 days back reads
complete, because it is (entry 350's trap).

**Cost.** Measured on the real store, an identity-only page of 500 visits takes ~0.4 s, so the
90-day walk is an estimated ~8 s. It runs on the shared write connection like `door_stats`
(entry 354), at most once per 10 minutes per model.

### Day states

The recording half is computed fresh on every request (cheap), never cached, so a Clear or an
eviction shows at once. It is hour-granular: one indexed existence probe per hour, never a walk of
every frame, since a Tuning day can hold 750k frames. The probes cover the 90 days plus the 7 hours
before them, so a gap that began before the window is measured whole.

The first matching row wins:

| state | when | drawn as |
|---|---|---|
| `expired` | the whole day is older than `store_oldest_ts` | no bar: "no longer kept" |
| `none` | no stored frame in the day | no bar: "not recorded" |
| `uncounted` | at or older than where a truncated count stopped | no bar: "not counted" |
| `unnamed` | the day has visits but none carries an `identity` under the active model | no bar: "not named yet" |
| `partial` | a run of 7 or more consecutive hours with no stored frame touches the day | the bar, in a distinct "at least" style |
| `full` | otherwise | a plain bar |

- **No-bar states.** The four no-bar states share one muted marker; only the readout and the note
  say which.
- **Runs cross midnight.** Runs are measured continuously, not cut at midnight. Every day a run
  touches is `partial`. A run still open at "now" counts too, so an outage happening right now
  marks today.
- **Why 7 hours.** Measured over the last 60 days with runs crossing midnight: ordinary runs never
  exceed 6 empty hours (four reach 5, two reach 6), and every outage measured 8–143 h.
  - Cutting at midnight would have missed two overnight outages outright: 09-24 19:00 for 11 h,
    and 10-10 22:00 for 8 h.
  - A very quiet night could still reach 7 and read `partial`. That is the safe direction, claiming
    less, and it is accepted rather than tuned.
- **What a partial bar means.** It shows what was recorded: a floor, not an estimate.
- **Why `unnamed` exists.** Names are read per model, and the live worker never names past visits
  (entry 337). So after a promotion, every past day would read about 0 until a whole-store Identify
  all reaches it, and while that pass runs, the 10-minute cache would show its moving front as a
  trend. A worker down with a sticky error leaves the same signature (entry 311). `unnamed`
  catches both.
- **What this misses.**
  - An outage under 7 hours still reads `full`.
  - A day an Identify all pass has only half-named reads `full` with a lower count.

### API

`GET /api/door-stats/daily` always answers with all 90 days; it takes no parameters. It serves
from an in-process cache:
- **Key and lifetime.** The key is (local date, active model id, threshold). An entry lives
  10 minutes.
- **Single-flight.** A request arriving while a count is running waits for that count rather than
  starting a second.
- **What is cached.** The counts and the `split` they were counted with, together. `split` is built
  exactly as `/api/door-stats` builds it: the same location and astral gate, and the same
  `available`/`reason`. Caching it with the counts means a location set within the 10 minutes can
  never pair `available: true` with day/night counts made without it. The recording states and
  `store_oldest_ts` are recomputed and merged on every request.
- **No gallery.** With no active model, or an uncalibrated one, it returns the day list with empty
  `cats` and the `model` field telling why, without walking at all.

### Page

- **Getting there.** On `#visits`, each cat row, resident or neighbour, becomes a link to
  `#visits/<cat_id>` with a trailing chevron. The router accepts that sub-route and keeps the
  Visits tab active. An unknown id shows "No such cat" with a link back.
- **Header.** "‹ Visits", the cat's avatar and name, and whether it is ours or a neighbour. The page
  fetches `/api/cats/overview` itself, since a reload or a shared link lands here without Visits
  having run.
- **Loading.** While the cold count runs, the page shows "Counting the last 90 days — a few seconds
  the first time", never a blank page. Responses are sequence-guarded, so a reply for a cat the
  reader has since left is dropped (entries 235/366).
- **Not in the model.** When `enrolled_cat_ids` is known and lacks this cat, the page says "The
  current model doesn't include <name>, so none of her visits can be counted", in place of the
  chart. A row of zeros there would be a reading of the build, not of the cat.
- **Window toggle.** A 30 / 60 / 90-day segmented control above the chart, 60 selected on entry.
  The choice holds while the app is open, not across reloads. Switching re-renders from the data
  already loaded.
- **Summary line:**
  - "About N visits a day", averaged over the window's `full` days only, leaving today out, and
    saying over how many (e.g. "over the 37 fully recorded days of the last 60").
  - The busiest day.
  - "as of HH:MM" from `generated_ts`.
  - With no `full` day at all it says so instead of averaging nothing.
- **Chart:**
  - One bar per day, in one colour: oldest left, today rightmost and labelled "today so far".
  - A y-axis scaled to the window's busiest day, with a few gridlines, and date ticks weekly
    (fortnightly at 90 days).
  - States drawn per the table above.
  - No horizontal scroll at 360 px wide in any window. At 90 days that is ~3.5 px a bar, so
    the gaps between bars may collapse to nothing.
- **Choosing a day.**
  - Bars are far too thin to tap, so selection hit-tests by x across full-height per-day columns,
    which includes the no-bar days. Tapping selects the day under the finger, and dragging scrubs
    across days.
  - The chart is one focusable control, where ← / → move the selected day.
  - The selected day shows in a readout under the chart:
    - "Sat 25 Oct — 85 visits (31 by day, 54 after dark)";
    - "partly recorded — at least 12";
    - "not recorded";
    - "no longer kept";
    - "not named by the current model yet";
    - "not counted (it was too busy to count this far back)".
- **Notes.** A note under the chart explains whichever markers are in the window, and appears only
  when one is. With no usable gallery the page shows the Visits page's own explanation instead of
  a chart.
- **Screen readers.** The SVG is `aria-hidden`, and a visually hidden table (date, visits, state)
  carries the same data. The readout is `aria-live="polite"`, so the keyboard path is heard too.

### Docs

- **ARCHITECTURE**, *Dashboard + API* row: the Visits page drills down to a per-cat daily chart
  over 30, 60 or 90 days, counted off the same feed and kept 10 minutes.
- **CONCEPT §6**, "how much": add "and day by day over the last few months".

## Alternatives considered

- **Count on every open.** It is always current, but every cat costs ~5–6 s even straight after
  the previous one. Rejected by choice.
- **Persisted per-day memo (`calendar_days` pattern).** It is instant even after a restart, but
  every path that can rename a past day needs an exact invalidation hook. Rejected by choice; it
  remains the upgrade if the cold count ever bites.
- **Stale-while-revalidate.** Serve the newest count at any age and recount in the background once
  it is older than 10 minutes. Every open after the first one following a restart would be
  instant, at the price of an older "today" bar. Not taken, because a plain 10-minute cache was the
  chosen trade. Note that an occasional visit to the page will usually find the 10 minutes lapsed
  and pay the ~8 s. If that is felt in daily use, this is the next step, and a smaller one than
  the persisted memo.
- **Caching each window separately, or slicing on the server.** A 60-day cold open would be
  ~5–6 s instead of ~8 s, but every first switch would wait for its own walk, and toggling would
  race in-flight requests. Rejected for one 90-day payload.
- **Client-side paging of `/api/events`.** Measured at ~17 s for 60 days (12 pages of the full
  feed payload) on a phone. Rejected.
- **Counting `identifications` per day directly.** It is cheaper, but it would name visits
  differently from the feed. Rejected for the same reason `door_stats` was built on `events()`.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** Three files on one thread (`store.py`, `app.py`,
  `web/user/index.html`). The page's states, notes and readout depend directly on the payload's
  `state` rules, and the shared paging helper touches `door_stats`, so splitting would mean two
  agents negotiating one contract. The `dataviz` and `frontend-design` skills apply to the page.

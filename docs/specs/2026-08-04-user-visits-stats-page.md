# The user dashboard's Visits page

A third view on the household app — **Activity · Visits · Cats** — replacing the
`Who's home` placeholder, which was blocked on direction detection and showed nothing.
Visits answers *how much* rather than *what happened*: per-cat visit counts over the
last 6 h and 24 h, each cat's day/night split and share of the door's traffic, and a
household summary that says how many visits went unnamed. It is a read-only projection
of the same event feed Activity renders, served by one new endpoint.

## Key decisions

- **Aggregated from `Store.events()`, not from `identifications`** (reuses). The new
  `Store.door_stats` counts the events the feed already produces, so every visit it
  tallies was clustered by `_gap_split`/`_VISIT_GAP_MS` and named by
  `_aggregate_identity`. This is the invariant `cats_overview` exists to protect
  (`store.py:6805`): Visits, Cats and Activity can never name the same moment
  differently, and the uncalibrated-model fail-safe carries over for free. A direct SQL
  aggregate over `identifications` would be cheaper and re-derive both the clustering and
  the vote — the drift this repo has already paid to avoid.
- **`GET /api/door-stats`** (new). Not `/api/stats` (taken — `Store.stats`, the store
  summary at `app.py:1203`) and not under `/api/visits` (taken — the *oracle-driven
  tuning* read, which this is not). The page's label and the route's name deliberately
  differ, because "visits" already means two things in this codebase.
- **One 24 h read serves both windows** (new). 6 h is a subset of 24 h, so the endpoint
  reads the 24 h window once and buckets each event into both windows by its `start_ts`.
  Two reads would cost twice as much and could disagree at the boundary.
- **Internal keyset paging, not a raised `_MAX_EVENTS`** (extends). `events()` clamps to
  500 events per call, and a busy 24 h can exceed that. `door_stats` walks pages exactly
  as the frontend does — `until_id` = (oldest loaded `start_id` − 1) — under a
  `_MAX_STATS_PAGES` budget, rather than widening a clamp that exists to bound a *feed*
  response. Page cost is flat (`CHANGELOG` 260) and each successive page's scan is
  narrower, since the window bounds it. Exhausting the budget sets `truncated`.
- **Three counts, because one would lie** (new). A per-cat `0` has three causes: the cat
  didn't come, nothing identified those visits, or the detector never looked at them. So
  the totals separate `unidentified` (clustered, no identity — no promoted model, or no
  identify pass over that span) from `unanalyzed` (no `yolo-serial` row at all) from
  `noise` (below the subject floor). Same rule as `CHANGELOG` 226/279: coverage decides
  what a reading may claim.
- **A "visit" is a cat visit, not a motion cluster** (new). `door_events` counts every
  cluster; `cat_visits` counts those with a cat identity or a `cat` subject. Share-of-
  traffic divides by *named* visits only. Counting wind-triggered clusters as visits would
  inflate every number on the page — and under continuous capture most clusters are noise.
- **Day/night mirrors the tuning split** (reuses). `night_classifier` + `Store.get_location`,
  as at `app.py:2097` and `app.py:2409`; a visit is bucketed **whole** by its `start_ts`,
  the same first-frame rule `gate_scorecard`'s split uses. No location or no `astral` →
  the split reports `available: false` with a reason and the UI drops those columns. It is
  never guessed.
- **Retention is stated, never assumed** (reuses). `Store.stats()`'s `oldest_ts` is two
  O(1) seeks, so the response says whether the store actually reaches back 24 h. On a ring
  buffer a "last 24 h" count over 9 h of retained frames is a partial, and a partial that
  doesn't say so reads as a quiet night.
- **No charts in v1** (new). Numbers and text only. `compute/CLAUDE.md` requires the
  `dataviz` skill before any dashboard chart, and nothing here needs one — a share
  percentage and a day/night pair read fine as figures. If bars or a sparkline are added
  later, that skill applies.

## Goals

- Answer "how often has each cat been through lately" without scrolling the feed.
- Make the unnamed remainder visible, so a low count is never mistaken for a quiet cat.
- Surface stranger and unknown-cat pressure as a number.
- Cost one bounded read per page load, flat in store size.

## Non-goals

- Occupancy / who's home. It needs direction detection; it returns as its own page then.
- Windows beyond 24 h, or a date picker. A 7-day column is the most interesting number
  and the most expensive — a wider scan, more pages, and a retention caveat that fires
  often on a ring buffer; it waits until this shape is proven. Activity owns browsing;
  `/admin` owns analysis.
- Any new persisted table, column, or background job. This is a read-time projection.
- Per-cat drill-down into the visits behind a count (Activity already plays them).

## Design

### Nav and routing

`user/index.html:780` gains a middle link and loses the last one:

```html
<a data-route="activity" href="#activity">Activity</a>
<a data-route="visits"   href="#visits">Visits</a>
<a data-route="cats"     href="#cats">Cats</a>
```

`ROUTES` (`:899`) becomes `['activity', 'visits', 'cats']`, `#view-home` and its
placeholder markup are deleted, and `onRouteEnter` (`:1471`) gains a `visits` branch
calling `loadVisits()`. An old `#home` bookmark hits `setRoute`'s existing unknown-route
fallback and lands on Activity — no alias, since a stale pin resolving to the feed is a
fine outcome and a redirect to Visits would be a lie about what it asked for.

The view refreshes on route enter, on foreground resume, and on the SSE nudge the page
already opens (`/api/events/stream`) — the signal is precisely "the feed changed", which
is precisely what these counts are derived from. All three already funnel through
`onRouteEnter(route)`, and nothing gates the nudge by route (the `route !== 'activity'`
test at `:2476` guards the infinite-scroll observer, a different mechanism), so wiring the
route in is the whole story. No timer of its own.

### `Store.door_stats(hours=(6, 24))`

Resolves `now − 24 h` to a `since_id` via `resolve_ts_range`, then pages `events()`
newest-first over `[since_id, None]` with `with_subject=True` until a page reports
`truncated: false` or the `_MAX_STATS_PAGES` budget (8 pages ≈ 4000 visits, far past a
real day at this door) is spent. Because `since_id` bounds the query, every returned event
is in-window, so `truncated` is the correct terminator. No visit is lost to the
partial-oldest-cluster drop `events()` performs when its frame scan caps: the next page's
`until_id` excludes only the *kept* oldest event, so a dropped cluster is re-read.

Each event is folded into whichever windows its `start_ts` falls inside, and classified
once:

| Bucket | Test |
|---|---|
| `resident` | `identity.cat_id` set and `is_resident` |
| `neighbour` | `identity.cat_id` set and not `is_resident` |
| `unknown_cat` | `identity` present, `cat_id` null (nearest match too far) |
| `unanalyzed` | no identity and `subject.kind == 'unanalyzed'` |
| `unidentified` | no identity, `subject.kind == 'cat'` — a cat nothing has named |
| `other` | no identity, `subject.kind` in {`person`, `corrupted`} |
| `noise` | no identity and `subject.kind` in {`unrecognized`, `motion_only`} |

The buckets are exclusive and sum to `door_events`, and `cat_visits` = `resident` +
`neighbour` + `unknown_cat` + `unidentified` — so every figure on the page is derivable
from the published totals rather than from a quantity only the server saw.

`unidentified` is deliberately cat-subject only: it is the number the page shows as "not
yet named", which must not include a person. A `person` visit lands in `other` alongside
`corrupted` and gets **no figure of its own** — the household page reports cats and
noise, and a person at the door is neither. The data is in `subject` if that ever wants
surfacing.

Per cat the record carries, per window, `visits` plus `day`/`night` (null when the split
is unavailable) and a `share` — that cat's visits over the window's named visits
(`resident` + `neighbour`), null when the denominator is 0. Every window carries `share`
so the per-window record has one shape; the page renders only the widest one's. No `last_seen`: the
page already fetches `/api/cats/overview` for avatars, and that field there spans the
whole retained feed while anything computed here would span only 24 h — two fields of the
same name meaning different things on one page.

The roster comes from `list_cats`, not from the events: a cat with zero visits still needs
a row, and `events()` reads `cats` only internally for naming.

### Response shape

```json
{
  "generated_ts": 1754300000000,
  "windows": {"6h": {"hours": 6, "since_ts": …, "covered": true},
              "24h": {"hours": 24, "since_ts": …, "covered": false}},
  "store_oldest_ts": 1754250000000,
  "model": {"id": 7, "calibrated": true},
  "split": {"available": true, "location": {"latitude": 55.68, "longitude": 12.57}},
  // when unavailable instead: {"available": false, "reason": "location_unset"}
  "totals": {"6h": {"door_events": 91, "cat_visits": 12, "resident": 9,
                    "neighbour": 2, "unknown_cat": 1, "unidentified": 0,
                    "unanalyzed": 0, "other": 0, "noise": 79},
             "24h": {…}},
  "cats": [{"cat_id": 3, "name": "Mittens", "is_resident": true,
            "6h":  {"visits": 4, "day": 3, "night": 1, "share": 0.36},
            "24h": {"visits": 11, "day": 7, "night": 4, "share": 0.34}}],
  "truncated": false
}
```

`covered: false` means the store does not reach back that far — the UI renders the count
with an "only back to hh:mm" note rather than as a whole-window figure. `model: null`
(nothing promoted) or `calibrated: false` (null threshold) means every visit resolves to
unknown, so the per-cat table renders its own empty state explaining that instead of a
column of zeroes.

### The page

Three stacked blocks, matching the existing warm user styling (`--porch` accent, the
`.cats-section` heading + `.lead` kicker pattern, `.cat-card` avatars):

1. **At the door** — the 6 h / 24 h household figures: door events, cat visits,
   strangers (`neighbour` + `unknown_cat`), and the honest remainder as a quiet line
   ("3 visits not yet named · 1 not analysed"), suppressed when both are 0.
2. **Our cats** — a row per active resident: avatar, name, 6 h and 24 h counts, day/night,
   share, and relative last-seen. A cat with no visits shows `—`, not a hidden row:
   "Mittens: none in 24 h" is the answer to a question someone actually asked.
3. **Neighbours** — the same rows for named non-residents, so a persistent visitor is
   countable. Retired cats are excluded, matching the Cats view.

The page fetches `/api/cats/overview` alongside the stats, as Activity already does, for
avatar URLs, their `avatar_version` stamps, and each cat's whole-feed last-seen.

### Cost

One `resolve_ts_range` seek, then 1–2 pages of `events()` over a 24 h window. The
window bounds the frame scan to that window's motion frames (~35 k/day at the measured
capture rate) rather than the feed's 200 k-frame tail, and the per-span annotation reads
stay proportional to the events returned (`CHANGELOG` 256/265 — the `+analyzer` hint is
inside `events()` already). Flat in store size.

## Alternatives considered

- **Client-side tally over `/api/events` pages.** No backend change, but a 24 h view
  costs 5–15 pages, each re-paying the feed's fixed 200 k-frame scan (`CHANGELOG` 271),
  and the aggregation would live in the frontend where it can drift from the backend's.
- **Direct SQL aggregate over `identifications`.** Cheapest, and the only option that
  scales to a 30-day window — but it re-implements clustering and the identity vote
  outside `events()`, which is exactly the divergence `cats_overview` documents itself as
  avoiding. Revisit only if long windows become the point.
- **Keeping `Who's home` as a fourth nav item.** Four links on a phone, one of which
  earns a tap and shows a placeholder. Occupancy comes back when it can answer.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Three pieces on one contract: `Store.door_stats`, the
  `/api/door-stats` route, and the view inside the single-file `user/index.html`. The
  frontend consumes the exact shape the backend defines, so splitting them would leave two
  agents guessing at one response body — and the honesty rules (what a `0` may claim,
  which bucket a subject falls in) are interpretation work, not transcription.

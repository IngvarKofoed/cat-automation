# Frame-range groups: scope tuning to a window of the store

Today every offline tool — the oracle sweep, the MOG2 baseline/candidate
re-run, the compare scorecards, the disagreement views — runs over the *whole*
store. This feature adds a **group**: a named, contiguous frame window
`[start_id, end_id]` that scopes all of those tools to just that slice, so you
can point YOLO / MOG2 / a scorecard at "the dusk visit that gets missed"
without sweeping hours of unrelated frames. A group is created by picking a
start and an end frame in the browse grid; it persists (name + bounds) so you
can re-select it across sessions.

The window is a *contiguous id range*, not a hand-picked set, for one
load-bearing reason: MOG2 and BSUV are **windowed/stateful** analyzers
(`compute/analysis/mog2.py:89`, `compute/analysis/base.py` `Analyzer.windowed`)
— they build a background model frame-by-frame and *must* be fed frames in
strict time order. A contiguous range is the only grouping that every analyzer
(stateless *and* windowed) can be swept over correctly. It also happens to be
the cheapest to build and the easiest to select (two clicks, not 500
checkboxes).

## Key decisions

- **A group is a contiguous `[start_id, end_id]` id range, not a membership
  set** (new). Frame row ids are the store's keyset axis and are monotonic in
  time, so a range is a first-class, cheap scope. Rejected an explicit
  `group_members` set because a scattered set can't be swept by the windowed
  MOG2/BSUV tuning tools — the primary thing we want to scope (see
  *Alternatives*).
- **Scoping the backend is by raw `since_id` / `until_id` bounds; the group
  table is a thin name→bounds bookmark on top** (new). The store, runner and
  scorecards learn nothing about "groups" — they gain a range floor/ceiling. A
  group is a saved `(name, start_id, end_id)` the *frontend* expands into
  bounds before calling. This keeps range-scoping a reusable store capability
  and lets an unsaved "pending range" preview use the exact same path.
- **Store iterators gain a `since_id` floor mirroring the existing `until_id`
  ceiling** (extends). `iter_time_order`, `iter_unanalyzed`, `count_unanalyzed`
  already cap at `until_id` (`compute/collection/store.py:455`+); adding
  `since_id` is the symmetric half and reuses the same keyset predicate.
- **A scoped windowed re-run warm-starts from the frames *immediately before*
  `start_id`, not from the newest frames** (extends). `MogAnalyzer._warm_start`
  today primes off a `_WARMSTART_ID` sentinel (newest N). Scoped, it primes off
  `recent_before(start_id, N)` so the model enters the window genuinely warm for
  that era. Requires the scope to reach the analyzer's `prepare`.
- **A scoped scorecard uses `warmup = 0`** (extends). The 500-frame warm-up
  prefix drop in `gate_scorecard` exists because an unscoped run starts
  cold-ish over the oldest frames. A scoped run is warm-started from the
  adjacent preceding frames, so its window is warm from frame one — dropping its
  first 500 would silently discard the very frames you selected to study.
- **`groups` is a new table in the same `index.db`, under the same single
  connection + single lock** (extends). One more table beside `frames` /
  `analysis`, same concurrency discipline as everything else in `Store`.
- **Group bounds do *not* cascade on eviction** (diverges). `analysis` rows
  cascade because each points at a specific frame; a group is defined by id
  *bounds*, which stay valid as endpoint frames age out — the window simply
  contains fewer live frames. A wholly-evicted group is reported as empty, not
  deleted.
- **Scope threads through the existing endpoints as an optional param; no
  parallel "grouped" endpoints** (extends). `/api/frames`, `/api/analysis/run`,
  `/api/tuning/rerun`, `/api/tuning/compare` gain optional bounds. Only group
  CRUD (`/api/groups`) is new surface.

## Goals

- Run an oracle sweep (YOLO/BSUV) over just a chosen window, so validating a
  suspicious slice costs seconds, not a whole-store pass.
- Run a MOG2 baseline/candidate re-run over just that window, *correctly* —
  contiguous order preserved, background warm on entry.
- Read the compare scorecards (recall, misses, false triggers, visits) for just
  that window, with live / baseline / candidate / oracle all scored over the
  same frames so the numbers are comparable.
- Browse and run the disagreement presets scoped to the window.
- Create such a window with minimal scrolling, name it, and re-select it later.

## Non-goals

- **Arbitrary hand-picked membership** (a group of scattered, non-adjacent
  frames). Deferred — it fights the windowed-analyzer constraint (see
  *Alternatives*).
- **Multiple disjoint ranges in one group** (e.g. "every night this week"). The
  natural v2; v1 is one contiguous window. Run per-window for now.
- **Curating a set for the training gallery / dataset.** That's the learning
  loop (`compute/dataset/`, `compute/learning/`), a later phase over a different,
  durable store — not this bounded, evicting tuning store.
- **Editing a saved group's bounds.** v1 is create / select / delete; to change
  bounds, make a new group.
- **Sharing / exporting groups between machines.**

## Design

### Data model

One new table in `index.db`, alongside `frames` and `analysis`:

```sql
CREATE TABLE IF NOT EXISTS groups (
  id        INTEGER PRIMARY KEY,
  name      TEXT    NOT NULL,
  start_id  INTEGER NOT NULL,   -- frames.id lower bound (inclusive)
  end_id    INTEGER NOT NULL,   -- frames.id upper bound (inclusive)
  start_ts  INTEGER NOT NULL,   -- recv_ts of the start frame, captured at create
  end_ts    INTEGER NOT NULL,   -- recv_ts of the end frame, captured at create
  created_ts INTEGER NOT NULL
);
```

`start_ts` / `end_ts` are denormalized on purpose: the endpoint frames may be
evicted later, and we still want to show the window's wall-clock span. `start_id`
is stored as `min(a, b)` and `end_id` as `max(a, b)` so the two endpoint clicks
can arrive in either order. New `Store` methods: `create_group`, `list_groups`
(each with a live `count = COUNT(*) FROM frames WHERE id BETWEEN start_id AND
end_id` — a fast primary-key range scan), `delete_group`.

### Range-scoping the store

The scoped reads all take an optional `(since_id, until_id)` — `None` meaning
unbounded on that side, so existing callers are unchanged:

- `iter_time_order(since_id=None, until_id=None)` and
  `iter_unanalyzed(analyzer, since_id=None, until_id=None)` /
  `count_unanalyzed(...)` — add the `id >= since_id` floor next to the existing
  `id <= until_id` cap.
- `query(...)` and `query_disagreements(...)` — add the same bounds to their
  WHERE, so the browse feed and disagreement view can be scoped. Keyset paging is
  unaffected (the cursor predicate ANDs with the range).
- `gate_scorecard(..., since_id=None, until_id=None, warmup=...)` and
  `gate_fidelity(slot, since_id=None, until_id=None)` — restrict the scored set
  to the range. The existing warm-up-prefix logic still works: it drops the
  first `warmup` rows *of the scored range*, and scoped callers pass `warmup=0`
  (see below).

### Runner and warm-start

`run_analysis` already snapshots `until_id = store.latest_id()`. Scoped, it also
carries `since_id` and clamps `until_id = min(group.end_id, latest_id())`,
`since_id = group.start_id`. It passes both to the iterator and to the total
count (`count of frames in [since_id, until_id]` for a windowed run;
`count_unanalyzed(..., since_id, until_id)` for a stateless one).

Warm-start needs the range's lower bound, so `Analyzer.prepare` grows an
optional `since_id`: `prepare(store, since_id=None)`. Stateless analyzers ignore
it. `MogAnalyzer._warm_start` (and `BsuvAnalyzer`'s equivalent) prime from
`recent_before(since_id, _WARMUP)` when `since_id` is set, else keep today's
newest-frames sentinel. `AnalysisManager.start_analyzer` /
`start` grow the same optional bounds and forward them; `status()` reports the
active scope so the UI can show "running MOG2 over <group>".

### Scorecard fairness (the subtle part)

A slot scorecard (`mog2:candidate`) only scores frames that carry a slot
verdict, because it INNER JOINs `analysis`. So after a *scoped* candidate re-run,
the candidate is implicitly scored only over the window — but `live` motion
(`frames.motion`) and the oracle exist for the whole store. An unscoped compare
would therefore score `live` over everything and `candidate` over the window —
mismatched denominators.

So the compare must be **explicitly scoped to the same window**: all four
columns (live, baseline, candidate, oracle) get the same `since_id` /
`until_id`, and `warmup=0` (the slots were warm-started before the window). The
intended workflow: *select group → Run baseline → Run candidate → Compare* —
each step carries the selected group's bounds, so the three re-runs cover the
window and the scorecard scores exactly it.

### API surface

New (group CRUD — the bookmark layer):

- `GET /api/groups` → `[{id, name, start_id, end_id, start_ts, end_ts, count}]`.
- `POST /api/groups` `{name, start_id, end_id}` → resolves the two frames'
  `recv_ts`, stores the group, returns it. 400 if either id is unknown.
- `DELETE /api/groups/{id}` → removes the bookmark (never touches frames).

Modified (optional bounds; absent = whole store, exactly as today):

- `GET /api/frames?...&since_id=&until_id=` — scope the browse / disagreement
  feed.
- `POST /api/analysis/run` body `+ since_id?, until_id?` — scoped oracle sweep.
- `POST /api/tuning/rerun` body `+ since_id?, until_id?` — scoped MOG2 re-run.
- `GET /api/tuning/compare?oracle=...&since_id=&until_id=` — scoped scorecards;
  the route sets `warmup=0` whenever bounds are present.

The frontend expands a selected group into `since_id` / `until_id` before each
call, so the backend stays group-agnostic and an unsaved pending range uses the
identical path.

### UI

A new **Scope** control and a small **Groups** panel, plus per-tile range
affordances. The scrolling problem is answered by *picking two endpoints*, never
by checking every tile:

- **Per-tile "Set start" / "Set end"** (small buttons in the tile caption).
  Clicking sets the pending range's lower/upper endpoint to that frame's id.
  Because ids are monotonic in time, defining a range is two clicks even across
  thousands of frames. (Endpoints are cleanest to pick in the default *time*
  sort; the id-range semantics still hold from any sort, noted in the panel.)
- **Groups panel:** the pending range's two timestamps and a live "N frames in
  range" readout (from `/api/frames` count or a lightweight count call); a name
  field + **Save group**; and a list of saved groups with a delete each.
- **Scope selector** ("Frames: All ▾ / <group>") at the top of the controls.
  Selecting a group applies its window *uniformly* to every feed view — the
  plain browse feed, both motion presets ("Missed?" / "False triggers"), and the
  disagreement views — *and* becomes the scope every Run / Compare uses, so
  "scope" means one consistent thing everywhere. "All" restores today's
  whole-store behavior. The MOG2 tuning panel shows the active scope so a
  re-run/compare can't be silently whole-store when you meant the window.

The pending range can be previewed in the grid (via `since_id`/`until_id` on
`/api/frames`) before saving, so you see what you're about to name.

## Alternatives considered

- **Explicit hand-picked membership set** (`group_members` join, checkbox +
  shift-range selection). Matches "add images to a group" literally, but a
  scattered set can't be swept by the windowed MOG2/BSUV tuners — the primary
  tools we're scoping — without either corrupting the background or running over
  the whole enclosing span anyway. It also needs cascade-on-eviction and its
  groups silently shrink. A viable middle path (arbitrary membership that scopes
  *scoring* only, while sweeps run the enclosing span) was rejected for v1 as
  more moving parts than a range delivers.
- **Saved-filter / predicate group** (a stored bookmark of motion/area/time/
  order). Generalizes the range, but any non-range predicate yields a
  discontiguous set and reintroduces the windowed-sweep problem; and it's less
  like the "a set of images" model. A pure time-range predicate is just this
  design with more surface.

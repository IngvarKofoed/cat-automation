# Activity page

A new user-facing `#activity` view in the compute SPA that turns the raw frame
store into a readable log of *what happened at the door*. Frames are bundled into
**events** — clusters of motion frames close in time — shown newest-first as a
date-scoped grid of thumbnails. Clicking a thumbnail opens a player that animates
the event's frames in time order, with play/pause and scrub. It is the no-oracle,
human-facing cousin of the existing (oracle-driven, tuning-focused) visit inbox,
and reuses the same clustering primitive and filmstrip rendering.

## Key decisions

- **Events = gap-split of *motion* frames** (extends). Cluster only
  `frames.motion = 1` via the existing `Store._gap_split()` with `_VISIT_GAP_MS`
  (2 s). The collector saves *every* frame continuously (~5–10 fps), so gap-splitting
  *all* frames yields one blob per session — the gaps only exist in the sparse
  motion frames. This is the load-bearing insight that makes "by time" work at all.
- **New `Store.events()` parallel to `Store.visits()`** (new). A separate read
  method rather than a mode on `visits()`, because `visits()` is entangled with
  oracle verdicts (missed/false/conflict) and warm-up semantics; the activity path
  needs none of that. Both share `_gap_split`, so clustering can't drift.
- **No new time-domain handling** (reuses). `/api/events` takes the same optional
  `since_id`/`until_id` scope every other windowed read uses; the client resolves the
  date filter to id bounds through the existing `/api/frames/resolve`
  (`Store.resolve_ts_range`), preserving the app's "resolve time→id in one place"
  invariant.
- **Playback reuses `/api/frames`** (reuses). The player fetches the event's frames
  with `/api/frames?since_id=&until_id=&order=time` and reverses to chronological —
  exactly what `loadFilmstrip()` already does — so no new media/frame endpoint.
- **Thumbnail = peak-area motion frame** (new). `rep_frame_id` is the cluster's
  highest-`area` frame (tie-break by id), not the middle-by-time frame — more likely
  to show the cat prominently. Area is already stored per frame; cost is nil.
- **Fourth SPA route, not a new app** (extends). Add `'activity'` to `ROUTES`, a nav
  link, and a `<section id="view-activity">`; reuse the design tokens, formatters,
  `addBounds`, and the visit-inbox stage/filmstrip patterns. No second frontend.
- **`events` naming vs. the domain `Event`** (new). These motion clusters are *not*
  the ARCHITECTURE `Event` entity (detection/enter/leave), which isn't built yet. The
  method/endpoint are named `events` for the user-facing sense; documented so the two
  aren't conflated. (Alternative: reuse "visit" — rejected to keep the user-facing
  page in the user's vocabulary.)

## Goals

- Answer "what happened at the door on <date>?" at a glance, over already-collected
  frames, with **no oracle sweep required**.
- Let the owner watch any event play out in time order, and study any single frame.
- Scope by date so a multi-day store stays navigable.
- Lay foundations that a later identification phase can layer cat-id and cat-id
  filtering onto without rework.

## Non-goals

- **Cat identity and filter-by-cat** — explicitly deferred to the identification
  phase; the event record leaves room for it but v1 shows none.
- **Oracle-confirmed / false-trigger filtering.** v1 clusters raw motion, so a leaf
  or shadow is an event too. Distinguishing real cats is the deferred oracle/cat work.
- **Enter/leave direction, occupancy, "who's home."** Those need identification +
  the door-zone geometry; not here.
- **Editing, labelling, or deleting events.** Read-only view over the frame store.

## Design

### Backend

**`Store.events(since_id, until_id, *, min_frames=1, limit=_MAX_EVENTS)`** →
`{"events": [...], "truncated": bool}`.

1. Under the store lock, fetch motion frames in the window:
   `SELECT id, recv_ts, area FROM frames WHERE motion = 1 [AND id range]
   ORDER BY recv_ts ASC, id ASC` (mirrors the `visits()` query shape).
2. `_gap_split(rows, _VISIT_GAP_MS, ts_of=lambda r: r[1])` into clusters.
3. Per cluster with `≥ min_frames` motion frames, build:
   ```
   {start_id, end_id, start_ts, end_ts, n_frames, rep_frame_id}
   ```
   `start_id`/`end_id` = min/max id of the cluster (the id span the player fetches);
   `n_frames` = motion-frame count; `rep_frame_id` = max-`area` frame (tie-break id).
4. Sort **newest-first** (`start_ts` desc). Cap at `_MAX_EVENTS` (e.g. 500), set
   `truncated` when the cap bites; clustering itself is cheap (over sparse motion
   frames only), so the cap bounds JSON/DOM size, not compute.

**`GET /api/events`** — params `since_id`, `until_id` (optional), `min_frames`
(default 1). Validates bounds via the existing `_validate_bounds`. No schema change,
no migration; purely a new read over `frames`.

### Frontend — the `#activity` view

**Date filter.** A **from/to** picker (start date + end date), defaulting to the
newest stored frame's day, mirroring the Buckets view's clock pickers. Start date
resolves to 00:00 of that day, end date to the end of that day (inclusive). On
change: build the bounds with the existing `tsFromControls`/`dateInputValue`,
resolve via `/api/frames/resolve` (`Store.resolve_ts_range`), then
`GET /api/events?since_id=&until_id=`. A missing/unmatched bound resolves to
unbounded on that side, so a lone start date means "from that day onward." (Day
resolution only — no time-of-day control; a later add if wanted.)

**Events grid.** One card per event: peak-area thumbnail (`/media/{rep_frame_id}`,
lazy-loaded), plus a caption — start time, duration (`formatSpan`), and motion-frame
count. Newest-first. Empty state: "No activity on <date>." A `truncated` result
shows "Showing newest N — narrow the date to see more."

**The player.** Clicking a card opens a detail/player region modelled on the
visit-inbox stage (large frame + filmstrip + prev/next), extended with:
- Fetch the event's frames via `/api/frames?since_id=start_id&until_id=end_id&
  order=time&limit=500`, reverse to chronological (reusing `loadFilmstrip`'s
  approach). If longer than `MAX_PLAY_FRAMES` (~240), **decimate evenly** client-side
  so preload and watch-time stay bounded (a long lingering cat need not play 600
  frames).
- **Preload** each frame into an `Image()`, then enable play.
- **Play/pause** auto-advances the large `<img>` through the sequence at a fixed
  ~8 fps (close to capture rate; a speed control is a later add), looping or
  stopping at the end.
- **Scrub**: a range slider `[0, n-1]` (pausing on input) and/or clicking a
  `filmTile` in the strip jumps to that frame; the current frame is highlighted and
  its timestamp shown.
- **Prev/Next event** and keyboard nav (space = play/pause, ←/→ = scrub or step
  events) following the inbox's `keydown` guard (ignore while typing, only on this
  route).

### What is deliberately NOT here

No motion-only-span caveat (this page *wants* motion frames, so motion-only capture
doesn't degrade it — unlike the tuning views). No warm-up prefix. No analysis
dependency, so the view is populated the moment any frames are collected.

## Resolved choices

- **Date filter:** from/to range picker (day resolution), resolved to id bounds via
  `/api/frames/resolve`.
- **Player:** in-view detail/stage panel reusing the visit-inbox pattern — no modal.
- **Noise threshold:** `min_frames` default 1 (show every motion cluster); a UI
  min-frames/min-area control is a later add if the list proves noisy.
- **Playback speed:** fixed ~8 fps; a speed control is a later add.

## Alternatives considered

- **Oracle-confirmed events** (cluster only YOLO-present frames). More accurate —
  drops false triggers — but empty until a sweep runs, and the real payoff (which
  cat) is the deferred identification work. Rejected for "for now by time"; the
  motion path can gain an oracle/cat filter later without restructuring.
- **A mode on `Store.visits()`** instead of a new method. Rejected: `visits()` is
  oracle- and warm-up-coupled; forcing a no-oracle user path through it would tangle
  the two. Sharing `_gap_split` gives the reuse without the coupling.
- **Autoplay-once on click / middle-frame thumb** (the original sketch). Refined to
  play/pause+scrub and a peak-area thumb after UX pushback — study-a-frame control and
  a more representative still, at negligible extra cost.

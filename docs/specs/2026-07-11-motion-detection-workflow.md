# Motion Detection workflow: pages, a walk-away queue, and scale review

The compute tier's browse UI is one long page (`compute/api/web/index.html`,
~1600 lines) that stacks stats, collector control, scope/groups, the
disagreement presets, the oracle-run panel, and the MOG2 tuning compare all at
once. It works for a few hours of frames but not for the real workflow: collect
continuously for ~24 h at ~10 fps (≈864k frames), then find the *handful* of
frames where the edge's MOG2 gate disagrees with the YOLO/BSUV oracles — misses
and false triggers — and tune MOG2 against them.

This feature restructures that page into three task-focused views — **Start**
(collection control), **Buckets** (define windows), **Motion Detection**
(validate + tune) — and layers on what the scale workflow needs: a **walk-away
job queue** so several buckets × oracles can be enqueued and drained unattended;
a **density timeline** and a **keyboard visit inbox** so the interesting frames
are findable without paging thousands of tiles; an optional **only-save-motion**
collection mode; and **persistence** so the collection mode and a resume prompt
survive a restart.

A **bucket** is the *existing* contiguous `[start_id, end_id]` group — the one
and only selection primitive. The stateful oracles require contiguity (see the
frame-range-groups spec), so a bucket is what you run YOLO/BSUV on, review, and
scorecard. The "interesting" frames (misses / false triggers / oracle conflicts)
are never stored — they are *computed on demand* from a bucket plus its stored
oracle verdicts, so no saved-selection object is needed (see *Alternatives*).

## Key decisions

- **A bucket is the existing contiguous `groups` window — the only selection
  primitive** (reuses). MOG2/BSUV are windowed/stateful and must be swept in
  contiguous order, so the whole workflow — oracle sweep, disagreement review,
  scorecard, MOG2 re-run — scopes to a bucket. Nothing new is persisted for
  "interesting frames"; they are a live query over the bucket + verdicts.
- **Three views live in the one `index.html`, switched client-side** (extends). A
  tiny hash router (`#start` / `#buckets` / `#motion`) shows/hides three view
  containers. No SPA framework, no separate HTML files — shared stats/polling code
  stays in one place. Bucket creation lives on Buckets; running + reviewing +
  tuning lives on Motion Detection.
- **Bucket definition is coarse → fine, not a scrollable full-frame grid** (new).
  At 864k frames a paged grid can't be scrolled to an endpoint. Instead: pick a
  rough era from **clock pickers** (date + time-of-day in 3 h steps for start and
  end), then place the exact endpoints on a **decimated thumbnail view** across that
  window. Because a bucket brackets slow-changing light/weather conditions,
  decimation loses nothing and the ±one-shown-frame boundary precision is ample.
- **One windowed frame viewer, with a density control, defines *and* inspects a
  bucket** (new/extends). The same viewer that places endpoints also reopens over a
  saved bucket for read-only inspection. Its **density control** picks how many
  images to show — *all* (the existing paged feed) or *X per minute* (a new
  `GET /api/frames/sample`) — so you never blindly load a huge window. A `recv_ts`
  index is added (the density timeline uses it too).
- **Analysis jobs run through an in-memory FIFO queue drained one at a time**
  (diverges). `AnalysisManager` today refuses a second job with 409; it now
  *enqueues*. That refusal lives in **three** places that all become enqueue: the
  manager's own busy-refusal (`RuntimeError`) **and** the route-level
  `if analysis_manager.running: 409` pre-checks in *both* `/api/analysis/run` and
  `/api/tuning/rerun` — the pre-checks must be removed or enqueue-while-running stays
  unreachable. Still exactly one active job (the GPU and the single SQLite
  connection are shared — serial is the only correct execution), so this is a
  status-list, not a scheduler. `/api/analysis/run` and `/api/tuning/rerun` return
  a queue position (deps checked synchronously at enqueue, so a missing-dep backend
  still 503s up front with its install hint). `status()` also carries a **finished-
  jobs history** (each job's terminal state — done / failed+error / canceled — with
  counts) so a returning operator can tell a clean drain from a silent partial
  failure. Three controls: **Cancel current** (skip the running job → advance),
  **Clear pending** (drop the queue, let the running job finish), **Stop all**
  (clear pending + cancel running, atomic — the unsurprising "stop everything").
  Internal contract only (our UI + tests consume it).
- **Collector mode + a resume prompt are remembered across restart via a
  store-owned `settings` KV** (new; keeps changelog 28). A small `settings` table
  in `index.db` (reusing the store's connection + lock) holds `motion_only` and the
  last `collector_running` intent. **Intent is written on the operator-initiated
  start/stop only — never by the process-exit shutdown hook.** `create_app`
  registers a shutdown hook that calls `collector_manager.stop()` on every exit, so
  a manager `stop()` that persisted intent unconditionally would record
  `collector_running=off` on every normal restart (Ctrl-C, upgrade, reboot — the
  common mid-run case), leaving Resume to fire only after a hard kill — the opposite
  of "collection survives a restart mid-run." So the intent write lives in the
  `/api/collector/start` and `/api/collector/stop` routes (or a `persist_intent`
  flag the shutdown hook passes as `False`), not in the bare `stop()` the hook
  calls. On a live launch the app restores `motion_only` but leaves the collector
  **stopped** — changelog 28's property (a bare launch never silently writes to the
  store) is preserved. When the intent was on, the Start page surfaces a one-click
  **Resume** so continuing is deliberate, not magic; `CAT_COLLECT_AUTOSTART=1` still
  forces begin-immediately for an unattended run.
- **Only-save-motion is a compute-side collector filter keyed on each frame's
  motion metadata** (extends). The decision lives in `run_collector` on the compute
  tier: for each received frame, if its metadata says motion it is `store.add`-ed,
  otherwise dropped. The edge keeps streaming *every* frame with its MOG2 motion
  flag as today — compute simply chooses whether to persist each one.
  **Caveat (load-bearing):** a *miss* is by definition a frame whose motion flag is
  `0` while a cat was present — dropping non-motion frames discards exactly those,
  so recall/misses become **unmeasurable** in that store. It is a compact
  false-triggers-only / cat-crop capture mode, **default OFF**, labelled as such.
  Because the toggle flips over time and leaves a *mix* of spans in one store, each
  transition is recorded (see *Motion-only spans* below) so any bucket / timeline /
  inbox overlapping a motion-only span is flagged **"misses unmeasurable here"**
  rather than silently reading as perfect recall — the false-confidence the oracles
  exist to prevent. **The damage is wider than the missed count:** BSUV and the MOG2
  re-run are windowed/stateful and assume temporally contiguous frames, but a
  motion-only span has arbitrary multi-minute gaps — so *their* verdicts (and the
  baseline/candidate scorecards built from them) are unreliable across such a span
  too, not just the `missed` tab. The flag therefore also warns at windowed-oracle
  *enqueue* over an overlapping bucket (see *Motion-only spans* and *The walk-away
  queue*).
- **The density timeline and visit inbox are new read endpoints** (new).
  `GET /api/timeline` bins a bucket's frames into per-bin counts (one grouped
  query); `GET /api/visits` returns per-visit records worst-first, *generalizing*
  the scorecard's counts-only recv_ts-gap clustering (`Store._cluster_visits`) into
  a shared helper that yields per-visit records. The inbox has three modes:
  **missed** / **false** (both judged against the **live edge gate** — the stored
  `frames.motion` flag, *not* a `mog2:*` re-run slot) and
  **conflict** (YOLO vs BSUV). Review only — **no persisted labels** (annotation
  is a later phase).
- **Everything else is reused unchanged** (reuses): groups CRUD,
  `query_disagreements`, `gate_scorecard`, `/api/tuning/compare`, `/media`,
  `/api/stats` — relocated under the new views, same behavior and window-scoping.

## Goals

- Collect continuously for ~24 h and **walk away** while YOLO + BSUV score one or
  several buckets unattended — a queue that drains serially with each job's outcome
  visible on return, while the collection itself survives a restart mid-run.
- From a bucket, locate MOG2-vs-oracle disagreements **without paging thousands of
  tiles**: a density overview to find dense error spans, a visit-level inbox to
  review them fast (representative frame + filmstrip context for the gate's warm-up).
- Define a bucket **by eye at 864k scale** — bracket a specific light/weather era
  (dawn, dusk, a rainy hour) without scrolling every frame — and inspect a saved
  bucket's content at a chosen image density before running oracles on it.
- Keep the tuning loop (scorecard/compare, MOG2 baseline/candidate re-run) reachable
  per bucket, unchanged.
- Give each phase its own surface: control collection (Start), define buckets
  (Buckets), validate + tune + review (Motion Detection).
- Offer an optional compact motion-only capture mode.

## Non-goals

- **Saved filter bookmarks ("review sets").** Considered and cut: the interesting
  frames are computed on demand from a bucket + its stored verdicts, so a saved
  filter persists a *query*, not results, and adds no capability over the three
  fixed inbox tabs plus on-the-fly filters. Trivially addable later if re-picking a
  filter each visit proves annoying.
- **YOLO stratified sampling / stride.** Deferred; a bucket gets a full YOLO sweep
  for now (BSUV can't be sampled — it's windowed).
- **Persisted human labels / annotation queue.** Later phase; the inbox reviews,
  it does not label.
- **Persisting the job queue or its finished-jobs history across restart.** Both
  are runtime-only (unlike collector state); written verdicts persist and YOLO
  resumes cheaply, so re-enqueuing after a restart is cheap.
- **Per-job queue removal, non-contiguous/multi-range buckets, a real scheduler**
  (priorities, parallel workers). Out — one GPU + one connection make serial
  correct; clear-all covers the queue.
- Any actuation, identification, or training.

## Design

### Pages / navigation

One `index.html`, three view containers toggled by a hash router; a top nav bar
switches them. Existing panels are redistributed, not rewritten:

- **Start** — the store stats badges + collector Start/Stop (`/api/stats` +
  `/api/collector/*`), the **only-save-motion** toggle + its caveat label, and the
  collecting badge. Both controls reflect the persisted state on load.
- **Buckets** — bucket *creation* via the windowed viewer below (clock pickers →
  decimated view → click start/end → name + Save, `POST /api/groups`), plus the
  saved-buckets list (live count + wall-clock span + **View** + delete), where View
  reopens the same viewer over a saved bucket. Replaces the old scroll-the-grid +
  per-tile "Set start/end" panel, which didn't scale.
- **Motion Detection** — the workbench. A **bucket picker** at the top; then, for
  the selected bucket: per-oracle coverage + **enqueue** buttons (YOLO / BSUV); the
  **queue panel**; the **density timeline**; the **error-mode tabs** (Missed /
  False / Conflict) feeding the **visit inbox**; and the existing scorecard/compare
  + MOG2 tuning panel. Everything on this page is scoped to the picked bucket.

### The windowed frame viewer (define + inspect a bucket)

The Buckets page centers on one **windowed frame viewer** — a thumbnail view of a
time window with a **density control** — used two ways: to *define* a new bucket
(clicking sets the endpoints) and to *inspect* a saved bucket (read-only).

**Everything scopes by `id` bounds**, uniformly with the rest of the app. The
clock pickers are the *only* time-domain input; they resolve **once** to
`[since_id, until_id]` via a small `recv_ts → id` lookup (nearest frame at-or-after
the start time, at-or-before the end time — served off the new `recv_ts` index).
Both density modes then run from those id bounds, so definition and inspection use
the same path.

**Density control** — how many images to show; the viewer never dumps a whole
window blindly:

- **All** — the existing paged feed (`/api/frames?since_id=&until_id=`,
  lazy-loaded thumbs). Correct even over a huge window because it keysets.
- **X per minute** — `GET /api/frames/sample?since_id=&until_id=&count=N` with
  `N = max(1, X × span-minutes)` (the frontend computes it; clamped to ≥ 1 so a
  sub-minute window can't ask for zero, and capped at ~a few thousand thumbs so a
  wide window can't request 80k). It returns evenly-spaced frames
  (`{id, recv_ts, url}`) via `ROW_NUMBER() OVER (ORDER BY id)`, `rn % stride = 0`,
  `stride = max(1, ceil(matched / N))`. Because light/weather move over
  minutes-to-hours, a low rate (1–6/min) already shows the window's whole arc.

**Defining a bucket** — two stages, both cutting the count shown:

1. **Coarse (clock).** Start/End pickers are `date + time-of-day` snapped to 3 h
   steps (00, 03, … 21 — a UI constant, easy to change), clamped to the store's
   `oldest_ts`/`newest_ts`. Narrows a multi-day store to an era, then resolves to
   the id bounds above — no images loaded yet.
2. **Fine (viewer).** The viewer shows that window at the chosen density; the user
   clicks one thumb for the start endpoint, one for the end, then names + Saves
   (`POST /api/groups`). The clicked thumbs are *real frames*, so their ids anchor
   the group exactly the way `create_group` expects — the density only limits
   *which* frames are offered, not the validity of the chosen ones. Boundary
   precision is ±one shown-frame gap, immaterial for a condition window.

**Inspecting a saved bucket.** Each saved-buckets row has a **View** that reopens
the same viewer over the bucket's stored `start_id`/`end_id`, read-only (no clock
resolution needed — the group already carries its ids), so you can confirm a window
holds what you meant before spending oracle time on it.

A `recv_ts` index is added to `frames` so the clock → id resolution here (and the
density timeline's `recv_ts` binning) is an indexed lookup, not a full-table walk.

**Optional extension (not built):** clicking a thumb could *zoom* — re-sample a
narrower span around it at finer density — for near-frame-exact endpoints. Left out
of v1 because condition buckets don't need it.

### The walk-away queue

`AnalysisManager` grows a pending FIFO (`collections.deque`) of job descriptors
`{kind, since_id, until_id, reanalyze, label}`, where `kind` is an oracle name
(`yolo`/`bsuv`) or a constructed MOG2 slot (`mog2:baseline`/`mog2:candidate` + its
params). `enqueue(job)` appends; if none is running it promotes the head. Enqueue,
external `start`, the finished-job promotion, **and cancel** all take the manager's
**one lock**, and the "clear `running` → promote next" transition happens *inside*
that lock as a single atomic step — so an external `POST /api/analysis/run` can
never observe `running=False` mid-promotion and double-start. The single-active-job
invariant is preserved — this turns *refuse* into *wait*.

**Cancel must be lock-guarded too.** `cancel()` today is a bare `stop_event.set()`
with no lock. Once promotion can *replace* `stop_event` as it advances to the next
job, an unlocked cancel racing that promotion can set the just-retired event (and
cancel nothing) or ambiguously hit the freshly promoted job. So `cancel()` (and the
cancel half of stop-all) reads and sets `stop_event` **under the manager lock**, and
targets the job that is `running` at the moment the lock is held — a cancel that
loses the race to a natural completion is a no-op, not a mis-fire against the
successor.

- **"Queue YOLO + BSUV for a bucket"** = enqueue two jobs (bucket scope × each
  oracle). **"Queue multiple buckets"** = enqueue across buckets. All drain serially.
- **Dedup key** is the *full* job identity: `(kind, params, reanalyze, since_id,
  until_id)`. For MOG2 slots `params` is part of the key, so re-queuing
  `mog2:candidate` over the same window with *different* params is NOT a duplicate —
  that is the tune loop and must be allowed; only an identical (same-params,
  same-window) pending/running job is dropped, to guard double-clicks. `reanalyze` is
  in the key too: a plain `yolo(bucket)` and a `yolo(bucket, reanalyze=true)` are
  *different* jobs — otherwise the re-verdict dedups away against the earlier run and
  silently never happens. Not deduped against *completed* verdicts — YOLO's
  `iter_unanalyzed` skips done frames, so a redundant (non-reanalyze) enqueue
  finishes near-instantly.
- **In-memory only:** a restart loses the queue but not the verdicts; interactive
  MOG2 reruns queue behind a running sweep — acceptable on one GPU, and scoped
  reruns are quick.
- **Motion-only overlap is flagged at enqueue for the windowed oracles.** Because
  BSUV and the MOG2 re-run assume contiguous frames, enqueuing one over a bucket that
  overlaps a motion-only span produces suspect verdicts (see *Only-save-motion* and
  *Motion-only spans*). The enqueue does not *refuse* — it succeeds but returns a
  `motion_only_overlap` warning alongside the queue position, so the UI can surface
  "verdicts unreliable across the motion-only span" rather than presenting the
  resulting scorecard as clean. (YOLO is per-frame and unaffected.)

**Outcomes & stopping.** `status()` (polled at `/api/analysis/status`) grows a
`queue` array (pending), the active job's `running`/`done`/`total`, and a bounded
`history` of finished jobs — each `{kind, since_id, until_id, state:
done|failed|canceled, error?, done, present}`. The worker records a job's terminal
state into `history` in the same lock-guarded step that promotes the next, so an
outcome is never overwritten by the successor (today's single `_error` field is
promoted to per-job records). The three controls map to: `/api/analysis/cancel`
(cancel running → the finally promotes the next), `/api/analysis/queue/clear` (empty
pending, running untouched), `/api/analysis/queue/stop-all` (clear pending *then*
cancel running, both under the lock so nothing is promoted). History is in-memory
like the queue — lost on restart, which is fine since verdicts persist and a
re-enqueue resumes cheaply.

### Persistence

A `settings` KV table in `index.db` (`key TEXT PRIMARY KEY, value TEXT`) holds
`motion_only` and the last `collector_running` intent. `Store` gets
`get_setting`/`set_setting`. The `collector_running` intent is written **only on an
operator-initiated start/stop** — from the `/api/collector/start` and
`/api/collector/stop` routes, not from the bare `CollectorManager.stop()` that
`create_app`'s process-exit shutdown hook calls. (If `stop()` itself persisted the
intent, the shutdown hook would flip it to `off` on every graceful restart, and the
Resume prompt would fire only after a hard kill — see the *Key decisions* entry.)
`motion_only` is written on toggle. On a live launch (`create_app`; tests still pass
`start_collector=False`) the app restores `motion_only` but leaves the collector
**stopped** — changelog 28's safety property (a bare launch never silently writes
to the store) is preserved. When the persisted intent was on, the Start page shows
a one-click **Resume collection (was on before restart)** prompt, so resuming is a
deliberate action rather than an automatic store-fill; `CAT_COLLECT_AUTOSTART=1`
still forces begin-immediately for an unattended run. The `settings` table is
**not** dropped by `clear()` — it is config, not frame data.

### Motion-only spans

To stop a motion-only span from silently reading as perfect recall, the collector
records each mode transition. A small append-only `mode_changes` table in
`index.db` (`at_id INTEGER, at_ts INTEGER, motion_only INTEGER`) gets one row
whenever `CollectorManager` flips `motion_only` (and one for the initial state on
first collect), stamped with the store's latest id + `recv_ts` at the flip. That is
a step-function over the id axis, from which a `Store` method returns the
motion-only *sub-ranges* overlapping any `[since_id, until_id]` (empty when the
window is wholly full-capture).

`/api/timeline` and `/api/visits` include that `motion_only_spans` list for their
window; the Motion Detection drill-down shows a **"misses unmeasurable here"**
banner and shades those timeline bins, and the `missed` inbox tab for such a window
is marked unreliable rather than reassuringly empty. Unlike `settings`, this table
*is* dropped by `clear()`: it is keyed to frame ids, and a full wipe reuses rowids,
so stale boundaries would misalign against new frames (the same reason `groups` are
dropped).

**`clear()` mid-run must re-seed the current mode, or the flag is lost.** `clear()`
does not stop the collector, and `settings` (holding `motion_only`) *survives* the
wipe while `mode_changes` is *dropped* — so a clear during a motion-only run leaves
`motion_only=true` active over an **empty** `mode_changes` table. Every frame
collected after that clear then sits in an *unrecorded* motion-only span, and the
timeline / `missed` inbox over it reads as reliable full capture — exactly the
false-confidence this table exists to prevent. So after `clear()` drops the table,
if collection is live it must immediately re-seed one row with the *current* mode
(latest id + `recv_ts` after the wipe). Equivalently the collector can lazily write
a mode row on the next `add` whenever `mode_changes` is empty — the same "initial
state on first collect" path, now also covering post-clear. (Resetting `motion_only`
off on `clear()` would work too but silently changes the operator's mode, so
re-seeding is preferred.)

### Density timeline (the overview)

`GET /api/timeline?since_id=&until_id=&oracle=&bins=N` → `{bins: [{t0, t1, total,
motion, present, missed, false}], motion_only_spans: [...]}` (the latter per
*Motion-only spans*, so the strip can shade windows where misses aren't
measurable). One grouped query bins the bucket's frames by
`recv_ts` across the window span (so quiet hours read as sparse bins, which
equal-id-count bins would hide); `present`/`missed`/`false` come from a LEFT JOIN
on `analysis` for the oracle and are zero when it has no verdicts. The frontend
renders a horizontal strip, one cell per bin, colored by disagreement (missed →
red, false → amber, else neutral) with intensity by count; clicking a bin narrows
the drill-down to that sub-window. A few hundred bins summarize the whole bucket
at a glance.

**No warm-up prefix is dropped, deliberately.** Because `missed`/`false` here (and
in `/api/visits`) judge against the *live* `frames.motion` flag — which the edge
produced from a MOG2 model that was already warm at capture time — there is no
un-primed prefix to discard, unlike `gate_scorecard`/`/api/tuning/compare`, which
*re-run* MOG2 offline and must drop the frames before the model has adapted. So
these two endpoints count every in-window frame, and their totals will legitimately
*differ* from a candidate/baseline scorecard's `visits.total` over the same window
(the scorecard omits its warm-up prefix). That divergence is expected, not a bug —
the inbox's "n / m" reflects the live gate, the scorecard reflects the offline
re-run.

### Visit inbox (the review)

`GET /api/visits?since_id=&until_id=&oracle=&mode=missed|false|conflict` → a ranked
list of visit records `{start_id, end_id, start_ts, end_ts, rep_frame_id, n_frames,
present_count, caught}`. **The `start_id`/`end_id` bounds are load-bearing, not
decorative:** `/api/frames` (and the filmstrip's frame fetch) filter by *id* only,
so a record carrying only a `ts` span gives the inbox no way to pull the visit's own
frames — the record must hand back the id window it clustered.
`Store._cluster_visits` today returns only `(total, caught)` *counts* and
clusters one predicate; `/api/visits` needs per-visit *records* over three
predicates, so the recv_ts-gap clustering is **generalized into a shared helper**
(the scorecard keeps calling it for its counts) — a real new clustering surface,
not a drop-in reuse. Per mode:

- `mode=missed` → cluster oracle-present frames; flag `caught`/wholly-missed (any
  source-motion frame within span ± `_VISIT_WINDOW_MS`); `rep_frame_id` = highest
  oracle-score frame. Surfaces the wholly-missed visits that cost a real trigger.
- `mode=false` → cluster motion∧oracle-absent frames; `rep_frame_id` = highest-area.
- `mode=conflict` → cluster frames where YOLO and BSUV verdicts differ (needs both
  oracles run); `rep_frame_id` = the higher-scoring oracle's frame. Ignores the
  `oracle` param — it compares the two.

**"Worst-first" is a defined sort** (the inbox's whole value): `missed` orders
wholly-missed visits before caught ones, then by `n_frames` desc (a longer missed
visit is a worse gate failure), tie-broken by peak oracle score desc; `false` and
`conflict` order by `n_frames` desc (longer spurious/disputed runs first),
tie-broken by peak area and peak score-gap respectively.

The inbox shows **one visit at a time**: the representative frame large, plus a
filmstrip of the visit's frames and a few *preceding* ones so the gate's warm-up
context is visible. `Store.recent_before` exists but returns bare filesystem
*paths* (for analyzer warm-start), whereas the filmstrip needs frame ids →
`/media/{id}` URLs — so this needs a **row-shaped sibling** (id + recv_ts + url),
not a drop-in reuse of `recent_before`; equivalently `/api/visits` can return the
per-visit context frames inline. The visit's own frames come from its `start_id`/
`end_id` via `/api/frames`. Keyboard-first: `←/→` (or `j/k`) move between visits,
progress shown "n / m". "Seen" is in-memory only — no verdict written.

### Reuse (unchanged)

The scorecard/compare panel and MOG2 tuning controls (`/api/tuning/compare`,
`/api/tuning/rerun`, `/api/edge/config`) move into the Motion Detection drill-down
but keep today's behavior and window-scoping. Groups CRUD, `query_disagreements`,
`/media`, and `/api/stats` are unchanged.

## Alternatives considered

- **Saved "review sets" — a second, predicate-based selection primitive
  (Approach B).** A saved bundle of filter params (motion / area / oracle / mode /
  time) driving browse + timeline + inbox. Cut because it persists a query, not
  results: the misses/false/conflict frames are already a cheap live query over a
  bucket + its verdicts (`query_disagreements`, `gate_scorecard`, the new
  timeline/visits endpoints), and the three views that matter are fixed inbox tabs,
  not arbitrary filters. It added a table + CRUD + a "tune panel greys out" special
  case for zero capability. Its param-expansion design makes it a trivial additive
  follow-up if named saved filters are ever wanted.
- **Bucket = a saved query, replacing groups (Approach C).** Rejected — a
  non-contiguous predicate can't be warm-started, BSUV-swept, or scorecarded, so it
  can't be the tuning primitive. A bucket stays a contiguous window.
- **A real job scheduler** (priorities, persistence, parallel workers). Rejected —
  one GPU and one SQLite connection make serial execution the only correct option;
  an in-memory FIFO with a status readout is the whole need.
- **Client-side visit clustering** (page `query_disagreements`, cluster in JS).
  Rejected — the server owns the clustering, and keyset paging fights a worst-first
  ordering; a small list endpoint is simpler.
- **Separate HTML pages or an SPA framework.** Rejected — three client-side views in
  the existing vanilla-JS page keep the toolchain and shared polling/stats in one
  place.
- **A separate `settings.json` file for persistence** (mirroring the edge).
  Rejected — a `settings` table in the store's `index.db` reuses the existing
  connection + lock and avoids a second persistence mechanism and its file-I/O
  error handling.

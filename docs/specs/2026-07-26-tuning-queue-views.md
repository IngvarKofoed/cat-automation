# Typed, unified queue views on the Motion-tuning page

Give the admin-next Motion-tuning page two queue panels — one under **YOLO coverage**
(coverage/oracle runs) and one under **MOG2 candidate params** (MOG2 baseline/candidate
reruns) — both drawn by a single shared, manager-agnostic renderer with fixed columns
`Status | Name | % done | x / y frames | FPS | Time to complete`. The two panels are filtered
*views* of the one serially-drained `AnalysisManager` FIFO, not independent queues. A
small `/api/analysis/status` extension (a per-job `category`, `since_ts`, `total`, and a
stable `job_id`) makes queued rows show real `x / y` and a per-row **Cancel**. Only the
running job shows FPS + a finish ETA (ported from the old `/admin`); queued rows show neither,
which sidesteps borrowing a rate across job types.

## Key decisions

- **`category` on every job** (new). `_Job` gains a derived `category` — `"mog2"` when
  `kind` starts with `mog2:`, else `"coverage"` — surfaced on the running job and each
  queue item in `/api/analysis/status`. Single source of truth so the renderer (and a
  later training-queue adopter) filters on a field, not on a `kind` prefix in the view.
- **Pending `total` computed at enqueue** (extends). `enqueue_named` / `enqueue_analyzer`
  compute each job's frame `total` right after `ensure_available()`, reusing the SAME rule
  the running job uses (`store.count_in_range` when `analyzer.windowed`, else
  `store.count_unanalyzed(...)` honoring `reanalyze` / `motion_only`), and carry it on the
  frozen `_Job`. It is an *estimate* (an earlier queue job can change the analyzed set
  before this one runs); `run_analysis`'s `set_total` remains authoritative once running.
- **Two filtered views of one FIFO** (reuses). The single-manager, one-job-at-a-time
  invariant (`runner.py:496`) is unchanged — no parallel queues, no second worker. Each
  card renders the subset of `running + queue` whose `category` matches it.
- **Shared manager-agnostic renderer** (new). One `renderQueue(el, rows)` in the
  admin-next SPA takes a normalized row list
  (`{status, name, pctText, done, total, fpsText, etaText}`) and draws the fixed-column
  table. Fed from `/api/analysis/status` here; shaped so the Model-building training queue
  could feed it later without change.
- **FPS + ETA on the running row only** (new here; reuses the `/admin` idea). admin-next has
  no ETA code today; port the `/admin` `etaAnchor` pattern (rate from progress across polls,
  re-anchored per running-job identity) for the single running job. Queued rows show no rate
  and no time — deliberately, so no queued row displays a borrowed cross-type rate. No
  cumulative-down-the-queue estimate.
- **Per-row Cancel via a stable `job_id`** (extends). Each job gets a monotonic `job_id` at
  enqueue, exposed in status; a `cancel(job_id)` manager method + endpoint cancels the
  running job (as `cancel()` does today) or removes a specific pending job from the deque.
  The renderer's rightmost column is a Cancel button per row. Reorder stays out of scope.
- **No cross-category indicator** (decided). A card shows only its own category's rows and
  the one worker is not surfaced across cards — accepted: a queued row on the coverage card
  while MOG2 runs shows no `Running` row (and the MOG2 Cancel lives on the MOG2 card). The
  queue stays truthful (never two `Running` rows), just not cross-referenced.
- **Capitalized status labels** (new). `Queued` / `Running` (and, if history is shown
  later, `Done` / `Failed` / `Canceled`) — capitalized in the renderer's label map.

## Goals

- Under each card, see that type's runs — the one active plus everything queued behind it —
  with per-row progress, a per-row **Cancel**, and (for the running job) live FPS + a finish
  ETA, instead of today's single "running … · N queued" line (`#jobLine`) and the MOG2 card's
  transient `#paramNote`.
- One renderer, one column contract, reused by both cards (and reusable by the training
  queue later).
- Never imply parallel execution: at most one row across both cards is ever `Running`.

## Non-goals

- **Parallel execution.** The two panels do not run concurrently; the single serial
  `AnalysisManager` FIFO stays. (That would be a separate, larger change — rejected.)
- **Wiring the training queue now.** The renderer is *designed* manager-agnostic; this spec
  only wires the two Motion-tuning cards. Model-building adoption is a later, trivial step.
- **Queue reorder.** Jobs run in enqueue order; the Cancel column can drop a pending job but
  not move it. The backend's existing clear-pending / stop-all endpoints stay unused here.
- **Terminal/history rows.** The table shows `Running + Queued` only; the last outcome keeps
  its existing one-line footer (idle / last error). History rows are a later extension.
- **Queued-row ETA/FPS.** Only the running job shows a rate and a finish estimate; queued
  rows show neither (so no borrowed cross-type rate is ever displayed). A per-category
  remembered rate — which a cumulative queued ETA would have needed — is not built.

## Design

### Backend — `/api/analysis/status` additions

`_Job` (`runner.py:447`, `frozen=True`) gains four fields, **all computed in
`enqueue_named` / `enqueue_analyzer` before the `_Job` is constructed** and passed to its
constructor — never mutated after init, so `frozen=True` is untouched (no `__post_init__`
assignment, no `object.__setattr__`):

- `job_id: int` — a monotonic id from a manager counter, assigned at enqueue; stable for the
  job's whole lifetime (pending → running). It is the handle the Cancel column targets — see
  the cancel API below — and is unambiguous where the dedup key isn't (e.g. two MOG2
  candidates whose params differ but which the status payload doesn't expose).
- `category: str` — `"mog2"` if `kind.startswith("mog2:")` else `"coverage"`. Note this is
  the *broad* oracle bucket, not "YOLO only": every non-`mog2:` kind (`yolo-serial`, and —
  because the FIFO is app-wide — a `bsuv` or `corruption` sweep launched from `/admin`, or
  Activity's `yolo-serial` *Analyze*) is `"coverage"` and shows in the coverage card. See
  the card-scope decision under Design.
- `since_ts: int | None` — `store.frame_recv_ts(since_id)`, computed at enqueue (off the
  store, outside the manager lock), so the Name column can label the job's target day.
  Deliberately **not** computed in `_status_locked`, which runs under the manager lock on
  every 3 s poll — a per-poll DB round trip there would couple the store lock to the manager
  lock and briefly stall the running sweep's `record()`.
- `total: int | None` — the estimated frames the job will process, mirroring what
  `run_analysis` will itself count (`runner.py:209`): `store.count_in_range(since, until)`
  when `analyzer.windowed` (MOG2/BSUV revisit every in-window frame, ignoring `motion_only`),
  else `store.count_unanalyzed(analyzer.name, since, until, motion_only)` for a stateless
  oracle. `reanalyze` does **not** switch a stateless job to `count_in_range` — it still
  counts the motion-scoped candidate set (a `motion_only` re-run processes only motion
  frames, so `count_in_range` would over-count ~20×); the pre-sweep clear just makes every
  candidate outstanding, which `count_unanalyzed` already reflects once cleared.

`_status_locked` (`runner.py:858`) then reports these four (`job_id`, `category`, `since_ts`,
`total`) on **both** the running job (read from `self._current_job`, which is a `_Job`) and
each queued job — so the Running and Queued rows format the Name (day + `· rerun`)
identically. The running row's live `done`/`total` still come from the manager counters
(authoritative once `run_analysis` calls `set_total`); the queued rows' `total` is the
enqueue-time estimate, which can drift if an earlier queued job analyzes overlapping frames
first.

**Cancel-by-id.** A new `AnalysisManager.cancel(job_id)` (all under the manager lock): if
`job_id` is the running job, set `stop_event` exactly as today's `cancel()`; if it matches a
pending job, remove it from the deque; if it matches neither (already finished/canceled), a
no-op. A new `POST /api/analysis/cancel/{job_id}` calls it. The existing bare
`/api/analysis/cancel` (cancel-running) stays for back-compat. Removing a pending job never
touches the running one, so the serial-drain invariant is unchanged.

Other than the new counter and cancel-by-id, no behavior change to enqueue/dedup/drain;
purely more fields on the snapshot, all additive (existing `/admin` + `/admin-next` consumers
ignore unknown fields).

### The two filtered views

In `mountTuning`, each card gets a `renderQueue` table. `pollJob` (already polling
`/api/analysis/status` every 3 s) builds, per card, an ordered row list:

1. the running job **iff** its `category` matches the card, then
2. the `queue` items with matching `category`, in FIFO order.

A queued coverage job sitting behind a running MOG2 job therefore appears in the coverage
card as `Queued`; no `Running` row shows there while MOG2 runs, and (decided) there is no
cross-card indicator of the shared worker. Cancel lives per-row in the table's rightmost
column (below): a queued row cancels itself out of the pending deque from either card, while
the running MOG2 can only be canceled from the MOG2 card, where its `Running` row lives.

**The queue table is not the whole card.** Today `#jobLine` and `#paramNote` carry more than
queue state, and those messages must keep a home — a per-card **status/note line** (kept
beside the table) still renders: enqueue-request failures (a `/api/analysis/run` /
`/api/tuning/rerun` that 503s on missing torch or 400s on a bad param produces **no** queued
job, so the table would otherwise show nothing); the MOG2 card's edge-seed line ("params
seeded from the edge" / "edge unreachable — using built-in defaults"); and the "pick a day
with frames first" validation. `pollJob` must also keep its current running→idle
`loadCalendar()` refresh (the `wasRunning` transition that updates the day's Y/B/C coverage
after a sweep finishes) — the rewrite adds the table, it does not drop that.

### The shared renderer and columns

`renderQueue(el, rows)` renders a table; each `row` is normalized to
`{jobId, status, name, pctText, done, total, fpsText, etaText}`:

| Column | Running | Queued |
|---|---|---|
| **Status** | `Running` (active pill) | `Queued` (muted) |
| **Name** | friendly kind + target day (`MOG2 candidate · Sat 25 Jul`), `· rerun` if `reanalyze` | same |
| **% done** | `round(done/total)%` | `—` |
| **x / y frames** | `done / total` | `0 / total` (est.) |
| **FPS** | live observed rate `r` (frames/sec) | `—` |
| **Time to complete** | ETA (see below) | `—` |
| **Cancel** | button → `POST /api/analysis/cancel/{jobId}` (stops the sweep) | button → same endpoint (drops it from the queue) |

`status → label` is a small capitalized map in the renderer (`Running` / `Queued`). The
frontend owns the kind→friendly-name map (`yolo-serial → "YOLO coverage"`,
`mog2:baseline → "MOG2 baseline"`, `mog2:candidate → "MOG2 candidate"`), **defaulting to the
raw `kind`** for anything unmapped — so a cross-page `bsuv` / `corruption` job that lands in
the shared FIFO renders as `corruption · <day>` rather than blank. `% done` guards
`total == 0` (→ `—`): during a job's `prepare()` (e.g. torch load) `total` is still 0, and an
unguarded `done/total` would show `NaN%`. The target day is formatted from each job's
`since_ts` (the day-scoped windows this page enqueues make `since_ts`'s date the job's day),
shown for every job type so a queue spanning several days stays unambiguous.

**Card scope (decided in-draft).** Each card filters by `category`, not by an exact kind
allow-list. So the coverage card shows *any* `"coverage"` job — normally only `yolo-serial`
(the sole coverage oracle this page and Activity's *Analyze* enqueue), but also a stray
`bsuv`/`corruption` job if one is running from `/admin`. That's acceptable: a stray job at
least explains why the one worker is busy, and the raw-kind fallback names it. The card keeps
its "YOLO coverage" title (accurate for the normal case); no rename.

### ETA + FPS (running row only)

Ported from `/admin`'s `etaAnchor`, applied to the single running job only — no cumulative
down-the-queue estimate, so no queued row ever displays a borrowed cross-type rate:

- **Rate `r`** (frames/sec) from the running job: anchor `{key, t0, done0}` on the first poll
  of a running-job identity, `r = (done − done0) / ((now − t0)/1000)` — frames processed over
  seconds elapsed since the anchor; the server carries no timing, so `r` is derived purely
  from progress across polls. Port the whole `/admin` `etaAnchor` faithfully, not just the
  idea: the identity **key** is `[analyzer, since_id, until_id, total]` and the re-anchor
  fires on `done < done0` too — two same-analyzer jobs promoted back-to-back with no idle
  poll between otherwise yield a negative rate. Dropped when idle.
- **FPS** column = `r`; **Time to complete** = `(total − done) / r`. Both only on the
  `Running` row. Show `…` while `r` isn't measurable yet (before the running job's second
  poll) or while `total == 0` (during `prepare()`).
- **Queued rows** show `—` for both FPS and Time to complete — they have no observed rate,
  and this is the deliberate choice (per the picker) that avoids ever showing a borrowed,
  potentially ~20×-wrong figure. Their `x / y` still shows `0 / total` (or `0 / …` when the
  estimate is `None`).

## Alternatives considered

- **Frontend-derived category (Option B).** Infer `mog2` vs `coverage` from `kind` in JS,
  no backend `category`. Smaller contract, but re-derived anywhere the renderer is reused
  and splits the categorization from the payload we're already extending for `total`.
  Rejected in favor of a single backend field.
- **Two independent queues / workers.** Would let a YOLO run and a MOG2 run proceed in
  parallel, but breaks the deliberate one-sweep-at-a-time invariant and adds GPU/VRAM
  contention. Rejected — the panels are views, not separate execution lanes.

## Implementation strategy

**Single agent, on the session (Opus) model.** The change is one tightly-coupled contract
across ~3 files — the backend `_Job`/status fields and `cancel(job_id)` and the frontend
renderer that consumes those exact fields, ports the `etaAnchor`, and wires the Cancel
column — small enough that a split would mostly risk the frontend guessing field shapes the
backend hasn't finalized. Keep it strong (not a cheaper tier) because the backend edits are
correctness-sensitive: a `frozen=True` dataclass, the `motion_only`/`windowed` count
semantics, the monotonic id, and cancel-by-id under the manager lock.

The **after-edits self-review** is the place to fan out: this touches manager concurrency and
a status contract, so review the aggregate diff across ~2–3 subagents (correctness/lock
discipline · status-contract & backward-compat · frontend renderer/ETA), each finding
verified before applying.

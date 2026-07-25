# Admin-next: streamlined compute admin UI

A ground-up replacement for the compute workbench (`/admin`), rebuilt as a
fresh standalone single-file page at a **new `/admin-next` route** so the
existing admin keeps working untouched until we flip at the very end. The
redesign collapses today's 8 hash-routed pages into **6** organized around the
real workflow — motion tuning, frame review, annotation, model building, plus a
retained Activity view — and folds the research scaffolding (Buckets, Sweeps,
Corruption) into the pages that actually use it. Styling is deliberately minimal (a plain dark theme) for now;
this spec is about structure and function, not looks. The backend `/api/*` is
reused unchanged; page *logic* is adapted from the old admin rather than
rewritten, so we shed structural complexity without throwing away working code.

## Key decisions

- **Parallel build, flip at the end** (new). New file
  `compute/api/web/admin-next/index.html` + a `@app.get("/admin-next")` handler
  mirroring the existing `/admin` one (`app.py:679`) — same `_SHELL_HEADERS`
  (`no-cache`, `app.py:669`), same "not built" 404 guard. The old `/admin` and
  its file are **not touched**. When admin-next is done the flip is swapping two
  `FileResponse` paths: new → `/admin`, old → `/admin-old` (delete once trusted).
  Routes changing mid-flight is acceptable.

- **Minimal dark theme, restyle later** (diverges). One small inline `<style>`
  block, plain dark palette, no design-token system. Deliberately diverges from
  the old admin's polished "review console" (changelog 43/65) — that polish is
  re-applied later, once the structure is settled.

- **Reuse `/api/*` unchanged; adapt, don't rebuild, page logic** (reuses). No
  backend rewrite. The surviving per-page logic (scorecard math, the
  sweep/queue, the annotation keyboard flow, frame overlays) is ported from the
  old admin into the new shell and adapted to the simplified structure. New
  backend work is additive only, and larger than a glance suggests — it spans:
  cleanup purges (+ purge-span recording), the "ignore" `dataset_items` label,
  the day/night sun-time split, per-cat regime coverage, an **identity read API**
  for Frame
  review's overlay (no such read endpoint exists today), the distance-sorted
  annotation queue (an `identifications` join), and CRUD for the compute lat/lon
  setting.

- **Persistence mode = the workflow phase, on the Start page** (extends).
  Reuses the existing compute-side `motion_only` toggle + `mode_changes` table
  (changelog 32). The Start page frames it as the phase control: *Tuning (keep
  all frames)* vs *Collecting (motion only)*. Phase 1 requires all-frames because
  measuring a gate **miss** needs the non-motion frame where a cat was present;
  motion-only makes misses unmeasurable — so Phase 1 is what *licenses* the
  disk savings of Phase 2+.

- **Scoped cleanup via the eviction accounting path** (extends). Purges route
  through the *same* cascade as size-based eviction (`_evict_locked`) — delete
  the JPEG + its `analysis`/`identifications` rows and decrement **all** the
  in-memory bookkeeping it maintains: `_count`, `_motion_count`, **and
  `_total_bytes`** (the retention cap depends on it) — **never a raw `DELETE`**,
  and **never** touch `dataset_items`, durable crops, or `model_versions`. A
  purge runs as a **batched background job** (progress + cancel, lock released
  between batches) like eviction, not a blocking request — a whole-store
  non-motion purge is millions of rows and must not hold the store lock (entries
  102–105). The orphan sweep walks the **frames media dir only**, never the
  dataset/avatar dirs (their files have no `frames` row and would look orphaned).

- **Tuning: YOLO-serial only, buckets → day-selection** (diverges). The tuning
  oracle is `yolo-serial`, surfaced simply as **"YOLO"**; BSUV and batched
  `yolo` are dropped from the UI. Saved buckets (`groups`) are replaced by a
  single **day** picker that resolves to an id window via the existing
  `resolve_ts_range`. No "all frames" option.

- **Day/night split is evaluation-only, powered by `astral` on compute** (new
  dep). A day's tuning scorecards are split into **Day** and **Night** at
  sunrise/sunset. `astral` (already a trusted pure-Python edge dep) is added to
  compute to compute sun times for arbitrary past dates; lat/lon comes from a
  compute setting seeded from the edge's `/api/night-light` config. The edge
  keeps a **single MOG2 param set** — a separate day/night edge param set is
  deferred, built only if the split scorecards diverge materially.

- **"Ignore event" = an `ignored` label on `dataset_items`** (extends). Rather
  than a new table, ignoring an event writes `dataset_items` rows with an
  `ignored` label and no crop files. This inherits, for free, the composite
  `(src_frame_id, src_recv_ts)` dedup/survival (changelog 57) — so `clear()` +
  rowid reuse can't mis-target it — and un-ignore via the existing Labelled
  review (changelog 58). The queue's "already decided" test then drops an ignored
  event automatically; ignore is **reversible**, never a permanent one-keystroke
  loss.

- **Activity page retained** (reuses). The old Activity view stays as the
  **visit-level** results + manual-backfill surface (recent named visits; the
  Identify pass; subject re-detect) — the complement to Frame review's
  frame-level inspector, and the only home for backfilling *historical* identity
  the live worker won't touch. Keeps the collapse at 8→6, not 8→5.

- **Frame review is also the model-evaluation surface** (new). One page to scan
  frames by time interval with per-frame overlays — motion box, YOLO box + conf +
  class, corruption flag, **and the identity match** — so it doubles as "what
  does the current model do on real frames." Folds in the old Corruption page's
  review features.

- **Validate-before-promote is retained** (reuses). Model building keeps the
  DINOv2 feasibility probe (kNN accuracy, confusion, same/diff AUC, suggested
  threshold — changelog 59/61) as the gate run **before** promotion. It is not
  dropped.

## Goals

- Replace the 8-page workbench with a 6-page UI whose structure mirrors the
  Collect → tune → review → annotate → build workflow (plus a retained Activity
  view), buildable in parallel without disturbing the working `/admin`.
- Make the operating **phase** (and its persistence mode) explicit and
  one-switch, with disk cleanup offered at the natural transition.
- Keep the motion-gate tuning loop (sweep → scorecard → tune → copy params to
  edge) but simplified to the one trusted oracle, and split day/night so an
  IR-night regression can't hide behind a good daytime number.
- Give annotation an "ignore," keep undo/relabel + crop quality grading, and
  surface per-cat day/night coverage so the IR-night gallery gets populated.
- Keep validation as the promote gate and warn when a resident has no night
  crops.

## Non-goals

- **No backend redesign.** `/api/*` and the store schema are reused as-is;
  only additive endpoints/columns for the new features.
- **No visual polish.** The dark theme is a placeholder; the design-token
  reskin is a later, separate effort.
- **No new oracles or ML.** BSUV/batched-YOLO are removed from the UI, not
  replaced. No new detector or embedder.
- **Event-scoped purges are deferred.** Cleanup ships only the non-motion purge
  + orphan sweep; dropping ignored/labeled events' source frames (low reclaim,
  Activity/last-seen side-effects) is a later addition.
- **No edge day/night MOG2 param switching in this pass** — deferred; revisit
  only if the Day and Night scorecards diverge materially.
- **The old `/admin` is not migrated or redirected** — it stays reachable until
  the flip; legacy bookmarks are not preserved after it.

## Design

### The shell

`compute/api/web/admin-next/index.html` is a self-contained document (own inline
CSS/JS, no sharing with `/admin` or `/` — the entry-80 convention). Hash-routed
nav across six routes, in workflow order (Activity last — a monitoring/backfill
view, not a pipeline step):

```
Start · Motion tuning · Frame review · Annotation · Model building · Activity
```

Served by a new handler cloned from `admin()` (`app.py:679`): same
`_SHELL_HEADERS`, same `.is_file()` guard against a missing file. A stopgap link
back to `/admin` sits in the nav during the transition.

### Workflow & modes (rendered on Start, but a system-wide model)

Four phases, stated plainly so the operator knows where they are and why the
order matters:

1. **Motion tuning** — persist **all frames** (required to measure gate misses).
2. **Annotation** — motion-only persistence is now safe (the gate is trusted).
3. **Build + promote model.**
4. **Active learning** — keep annotating; relabel mistakes; uncertain run-mode
   matches feed back into the queue.

Persistence mode follows the phase and is the compute-side `motion_only` toggle
(changelog 32). The mode control lives on Start.

### Page 1 — Start

- **Phase description** — a short blurb naming the four phases and the
  all-frames→motion-only rationale.
- **Collection controls** — start / stop / resume the collector
  (`/api/collector/*`, changelog 28/31 — a bare launch never writes until the
  operator starts it, so this is the only way to begin collecting), the
  live-identify toggle, and **Clear all frames** (`clear()`, behind a confirm).
  These carry over from the old Start page and must not be lost in the collapse.
- **Mode control** — *Tuning (keep all frames)* vs *Collecting (motion only)*,
  writing `motion_only` (recording a `mode_changes` row as today).
- **Active model indicator** — the current `active` `model_versions` row and its
  confidence threshold, so what's live is always visible.
- **Location (lat/lon)** — the compute-side setting the day/night split needs,
  seeded once from the edge's `/api/night-light` config and editable here. If it
  is unset (edge never configured, or unreachable at seed time), the day/night
  split is shown **unavailable with a "set location" prompt** — never silently
  defaulted (a wrong location gives confidently-wrong Day/Night columns).
- **Store stats** — frame/motion counts + recv_ts span from the O(1) `stats()`
  (changelog 104).
- **Cleanup** — two scoped purges, each showing an estimated reclaim before
  acting, each a batched background job (Key decisions):
  - *Drop non-motion frames* (older than a date, or whole store) — the big
    reclaim (~95% of bytes); the main post-tuning action. **Records a purge
    span** (a `mode_changes`-style marker) so later scorecards/coverage over that
    window warn "misses unmeasurable here" — exactly as motion-only spans do
    (changelog 32); without it a purged day would read a false ~100% recall.
  - *Sweep orphaned files* (JPEG with no row — the changelog-42 leak).

  The event-scoped purges (ignored / labeled events' source frames) are
  **deferred** — low reclaim, and they'd erase visits from the user Activity feed
  / regress "last seen" (`cats_overview`, changelog 81). The non-motion purge is
  **also offered inline at the Tuning→Collecting switch** ("Switching to
  motion-only — drop the N non-motion frames from tuning? (~X GB)").

### Page 2 — Motion tuning

Numbers, not frames (frame inspection is Frame review's job; a miss links out to
it). Scope is a **day** (resolves to `[since_id, until_id]`), never "all frames".

- **Oracle sweep** — enqueue **YOLO** (`yolo-serial`) over the day via
  `/api/analysis/run`. Keep the job queue (progress / ETA / cancel — changelog
  30/39/52) and the coverage readout (`/api/analysis/coverage`) so a scorecard
  is never read against thin coverage.
- **MOG2 candidate** — the six param fields with per-knob hints (changelog 79).
  Enqueue **Baseline MOG2** (edge's live settings via `/api/edge/config`) and
  **Candidate MOG2** (edited knobs) offline re-runs (`MogAnalyzer`, changelog
  23).
- **Evaluation** — visit-recall scorecards for live gate / baseline / candidate
  (`/api/tuning/compare`), **split into Day and Night** at sunrise/sunset. A miss
  count links to those frames in Frame review (deep-link carrying the id span +
  a "misses only" filter).
- **Winning params → edge** — the candidate's params shown for copy-paste into
  the edge config UI (the actual output of tuning).

Day/night split rules (the metric is **visit** recall, changelog 46, so the
split is per-visit, not per-frame): a visit is assigned to the bucket of its
**first present frame**, so a dusk/dawn-straddling visit counts once, in one
column — never split into two half-visits. The scorecard's warm-up prefix
(`gate_scorecard`) is applied **once** before splitting. `/api/tuning/compare`
(and the scorecard builder) gains an optional split param that buckets by
`recv_ts` via compute `astral` sun times; absent the param, output is unchanged.
If no location is configured (see Start), the split is unavailable — the
scorecard stays single-column rather than guessing a boundary.

### Page 3 — Frame review

Scan stored frames by **time interval** with filters, each frame carrying all
its known info as overlays:

- **Motion** — the gate's box/area/flag.
- **YOLO detection** — box + confidence + class (cat/person/bird), from the
  stored per-frame detection (`/api/frames/sample?detections=yolo-serial`,
  changelog 111) — the same payload the playback filmstrip already consumes.
- **Corruption** — the stored `corruption` flag (the old Corruption page's
  review view folds in here). The chroma-based detector won't fire on IR-mono
  night; it is kept as-is (harmless on mono) pending the IR camera, when it may
  be reworked or retired.
- **Identity** — the frame's nearest-gallery match (`identifications`, changelog
  68), making this page double as model evaluation.

Filters: motion-only / misses / false-triggers / corrupt / has-identity, over a
resolved time window. This subsumes the old density/buckets viewer, the
Corruption page, and the playback overlays into one inspector.

### Page 4 — Annotation

Keyboard-first per-visit labelling, fed by **both** raw collected events **and**
low-confidence run-mode matches, worst-first (the active-learning loop).

- **Queue** — non-annotated events (clustered via `_gap_split`, floored at the
  detection conf floor — changelog 73), **bounded** (paginated / recent-window,
  not an unbounded whole-store scan — `annotation_visits` has a known scaling
  limit, and entries 102–104 exist to kill O(store) scans). Ordering: newest-
  first before a model exists; once a model is active, run-mode matches **below
  the model threshold** (an `identifications` join) sort worst-first by distance,
  with never-identified events after them.
- **Ignore** — a key that writes an `ignored` `dataset_items` label (no crop)
  and drops the event from the queue; reversible via the Labelled review, like
  any other label (Key decisions).
- **Undo / relabel** — the existing Labelled mode (review newest-first,
  re-label with 1–9/u/x, send back with `d` — changelog 58), with its crop-file
  cleanup preserved so the durable set never drifts.
- **Crop quality grading** — gallery/ok/poor per frame (changelog 57), retained
  because Model building's gallery-build filters by it.
- **Per-cat day/night coverage** — a readout like "Mittens — day 42, night 0"
  (split labeled crops by `recv_ts` vs sun times) so you know which regime to go
  capture.

### Page 5 — Model building

- **Build** — `gallery-build` from selected-quality `identified` crops
  (changelog 67), on the `TrainingManager` queue. A **single gallery holds both
  day and IR-night crops** per cat (not separate per-regime galleries) —
  escalate to two galleries only if cross-regime matching proves too weak in
  validation.
- **Validate** — the DINOv2 feasibility probe (changelog 59/61) as the
  pre-promote gate: kNN accuracy, confusion, same/diff AUC, suggested threshold,
  gallery/ok/poor A/B, in-page report.
- **Promote** — flip target→active, current-active→retired (changelog 67).
  **Warn (never block) if any resident has no night crops** — split that cat's
  labeled crops by `recv_ts` vs sun times; an empty night set means that cat is
  unrecognizable after dark. Warn, not block, because pre-IR-camera there may be
  *zero* night crops for anyone, and blocking would brick promotion entirely; a
  hard block can become an opt-in toggle once night data exists.
- **Threshold + versions** — set the confidence threshold on the model row
  (applied at read, always tunable); keep the version list visible so rollback
  is "promote a retired one."

### Page 6 — Activity

Retained from the old admin as the **visit-level** companion to Frame review
(which is frame-level): recent door events as cards with their identity chip
(resident / neighbour / unknown / subject), click-to-play, reusing `/api/events`
and the playback overlays. It is also the only home for the **manual backfill**
jobs, which have no other trigger:

- **Identify pass** (`/api/identify/run`) — the only way to name *historical*
  visits; the live worker only names new ones (changelog 74/75). Runs over the
  shown window.
- **Analyze / subject-backfill** (`reanalyze`, changelog 90/91) — re-detect a
  window so old events gain person/bird/cat subjects.
- **Per-event re-analyze** (changelog 107) — re-detect one open event's frames.

Backfill jobs enqueue on the existing sweep/training queues; progress shows on
the relevant page. Distinct from the user dashboard's `/` Activity feed — this
one carries the admin backfill controls. Placed last in the nav: a
monitoring/backfill surface, not a pipeline step.

### The flip (end state, not this increment)

When admin-next is complete: point `/admin` at `admin-next/index.html`, move the
old file's route to `/admin-old`, and later delete it. One small `app.py` edit;
no data migration.

## Alternatives considered

- **Rename old pages in-place to `_old` and build new views in the same file.**
  Rejected — the admin is one 354 KB document with shared globals/CSS; old and
  new would collide on state and styling. A fresh file at a parallel route is
  isolated and lets the old admin keep working byte-for-byte.
- **Rebuild every page's logic from scratch.** Rejected — the simplification
  wanted is structural, not logical; the scorecard/annotation/overlay code is
  proven and worth adapting rather than reinventing.
- **Fixed clock time for the day/night split.** Rejected — wrong across seasons;
  `astral` gives correct per-date sun times cheaply and reuses an edge-proven
  dependency.
- **Keep BSUV / batched YOLO as selectable oracles.** Rejected — `yolo-serial`
  is the trusted oracle; batched YOLO over-detects and BSUV is CUDA-heavy. Fewer
  moving parts is the point.

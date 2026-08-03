# Operating stages: one setting instead of four switches

The compute tier moves through three stages — tune the motion gate, collect and
annotate, then just run — and today it has no idea which one it is in. What exists
instead is four independently-persisted switches in the settings KV
(`collector_running`, `motion_only`, `yolo_oracle`, `live_identify`) that the
operator hand-assembles, with the stages written out as *prose* in the Start
page's "Workflow phases" card (`compute/api/web/admin-next/index.html:958`).
This spec makes the stage real: one `stage` setting that owns capture mode and
the always-on workers, and a detection floor that holds in every stage.

The gap it closes is a hole in stage 2. `YoloOracleManager._tick` bails outright
under motion-only capture (`compute/learning/yolo_oracle.py:277`) and
`LiveIdentifyManager._tick` bails when `active_model()` is `None`
(`compute/learning/live_identify.py:290`) — so between switching to motion-only
capture and promoting a first gallery, **nothing detects anything**. New visits
land as `unanalyzed`, the annotation queue stays empty, and the operator's only
recourse is a manual sweep from the Motion-tuning page. Nobody notices until the
queue they were about to work is blank.

`docs/ARCHITECTURE.md` already claims a `Mode` entity ("the current operating
mode (`collection` | `run`), as system state") that has never existed in the
code. This closes that drift too, at three stages rather than two.

## Key decisions

- **`stage` is the single persisted intent** (new). One settings-KV key holding
  `tuning` | `collecting` | `running`. The `yolo_oracle` and `live_identify`
  intent keys stop being read — the stage decides — and `motion_only` becomes a
  value the stage *writes* rather than an independent switch.
- **The collector's start/stop stays independent** (diverges). Pausing capture is
  not a stage, and folding it in would let a stage flip silently begin writing to
  the store (against entry 28's property) or clobber the resume intent of entry
  31. Stages describe how frames are handled *while* collecting.
- **Detection runs in all three stages; coverage follows what is stored**
  (extends). The bail at `yolo_oracle.py:277` becomes a `motion_only=`
  pass-through into `run_analysis`, which already accepts it
  (`compute/analysis/runner.py:146`, threaded to `iter_unanalyzed` since entry
  91). Full coverage in `tuning`, motion coverage in `collecting`/`running`.
- **Coverage mode is read live per tick, never applied via stop/start** (reuses).
  `YoloOracleManager.start()` re-seeds the watermark to the frame horizon on
  *every* call (entries 149/150), so driving a stage change through stop→start
  would silently discard whatever tail the worker had not yet drained. It follows
  the shape `motion_only` already uses: a zero-arg getter read fresh each tick.
- **The worker is renamed the detection worker** (diverges). "Oracle" is a
  motion-tuning word for a thing every stage now needs. Presentation only —
  `_ANALYZER = "yolo-serial"`, the settings keys, and the `analysis.analyzer`
  values are untouched, exactly as entry 147 kept the slug while relabelling it.
- **`live_identify`'s no-model bail stays** (reuses). With detection guaranteed by
  the detection worker, there is no reason to split its detect and identify
  halves — so its watermark keeps the load-bearing "advance only after BOTH
  passes complete" rule, and naming keeps its own failure domain.
- **Per-worker start/stop endpoints survive, unlisted** (extends). They leave the
  Start page but stay curl-able for debugging, the same call entry 221 made for
  the corruption endpoints when their UI was deleted.
- **Stage 3 is an assertion, not a distinct configuration** (new). It runs the
  same workers as stage 2; what it adds is *no human input is expected here*,
  which the UI reflects. Stated rather than dressed up as a third recipe.
- **Auto cleanup is driven by disk pressure, not by a stage change** (new). The
  store is already a fixed-size ring; the stage decides only *what it sheds
  first*. So switching stages destroys nothing, and no button has to be pressed
  either.
- **Eviction prefers non-motion frames outside `tuning`** (extends).
  `Store._evict_locked` (`store.py:722`) currently reclaims strictly by ascending
  id regardless of motion. Outside `tuning` it reclaims non-motion frames first,
  which keeps annotatable motion history for the same disk.
- **Selective eviction records a `nonmotion_evicted_through` watermark**
  (extends). A settings-KV integer folded into `Store.motion_only_spans` as a
  prefix span — not a `purge_spans` row per batch, which eviction's continuous
  small batches would bloat. Without it, a partially-stripped window reads as good
  recall from an absence of evidence.
- **The orphan sweep runs automatically at launch** (extends). An orphan is a
  JPEG with no row: unreachable by construction, so collecting it loses nothing
  and needs no marker, confirmation, or stage awareness.

## Goals

- Make the stage a thing the system knows, so an invalid combination of switches
  is not reachable — in particular the stage-2 no-detection hole.
- Keep YOLO detection running in every stage, so subjects, per-visit aggregates,
  the annotation queue, and identification always have verdicts to read.
- Give the household a way to say "we are done annotating" that the console
  respects.
- Stop routine cleanup being something the operator has to remember, without
  making any stage change destructive.

## Non-goals

- Any change to the edge. It streams every frame regardless; motion is a header
  signal, and no stage reaches the Pi.
- Destroying data *because* a stage changed. Auto cleanup is driven by disk
  pressure; the retroactive non-motion purge stays a deliberate, separately
  confirmed job.
- Retiring the manual jobs. A backfill sweep, a manual Identify, and the purges
  stay available in all three stages.
- Notification policy per stage — the notifier does not exist yet.

## Design

### The stages

| | **`tuning`** | **`collecting`** | **`running`** |
|---|---|---|---|
| Frames saved | **all** | motion only | motion only |
| Detection worker | on, **full coverage** | on, motion coverage | on, motion coverage |
| Live-identify | on (idles until a gallery) | on | on |
| Scorecard: false triggers | measurable | measurable | measurable |
| Scorecard: misses | measurable | **unmeasurable** | **unmeasurable** |

Keep-all belongs to `tuning` alone because a gate *miss* is a non-motion frame
that in fact held a cat — visible nowhere else, and unrecoverable once the frame
was never written. That single fact is what licenses the disk cost, and it is
why `tuning` is the cold-start default.

Note the two scorecard rows: a false trigger is a *stored* motion frame with no
cat, so that half stays valid in every stage. Only the miss column goes dark, and
`Store.motion_only_spans` (folding in `purge_spans`) already banners the windows
where it does.

### What a stage change does

`POST /api/stage` with `{stage}` performs, in order:

1. `CollectorManager.set_motion_only(...)` for the stage's capture mode — **not**
   a direct `set_setting`, because that method is what records the
   `mode_changes` boundary row (`Store.record_mode_change`) that
   `motion_only_spans` later reconstructs the unmeasurable windows from.
2. Persist `stage`.

The detection worker needs no call: it reads its coverage mode from a live getter
each tick, so the flip in step 1 is picked up on the next tick with its watermark
untouched. Both workers keep running across every stage change.

Entering `collecting` additionally surfaces a **link** to the existing Cleanup
card, as an explicit "reclaim now" — the operator switching stages is the one
moment they know the keep-all window is finished with, and preferential eviction
(below) may not need the space for weeks. A link, never a run: an inline
confirm-then-purge would put irreversible deletion one dialog away from a mode
toggle, which is the coupling this design rejects.

### The detection worker's coverage mode

The bail becomes a pass-through. Where `_tick` currently returns early, it instead
resolves `motion_only = self._motion_only()` once per tick and hands it to each
chunk's `self._detect(...)` call, leaving the `_MAX_FRAMES_PER_TICK` / `_CHUNK`
windowing, the watermark discipline, and the `is_busy` yield exactly as they are.

**Known limit at a `tuning` → `collecting` transition.** Frames captured
keep-all but not yet reached by the worker get motion-swept, and the watermark
then passes their non-motion siblings permanently un-swept. One watermark cannot
express two coverage levels, and a second one is more machinery than the case
deserves: those are precisely the frames the operator is about to drop. It fails
in the direction the codebase already documents (entries 142/149: coverage is
forward-only; older windows are the manual sweep's job). The reverse transition
is clean — `collecting` stored no non-motion frames to have missed.

### Redundancy between the two workers

Under motion-only capture the detection worker and live-identify cover the
identical frame set: every motion frame belongs to exactly one gap-split cluster,
so "all motion frames" and "all closed visit spans" are the same set, and both
write `yolo-serial` rows into the same `analysis` table. That overlap is
deliberate and nearly free — both walk `iter_unanalyzed`, so whichever worker
reaches a frame first, the other queries and moves on. Entry 139 already
establishes the overlap as idempotent (`INSERT OR REPLACE` on
`(frame_id, analyzer)`, all writes serialized on the store lock).

What running both buys is an independent detect path: live-identify advances its
watermark only after detect *and* identify succeed for a span, so an identify
fault would otherwise stall detection along with naming.

### Auto cleanup

Cleanup today is entirely manual: two operator-initiated `CleanupManager` jobs
(non-motion purge, orphan sweep) plus indiscriminate byte-cap eviction. Sorting
the work by **reversibility** gives three tiers, and the stage decides only the
middle one.

**Fully automatic, every stage — the orphan sweep.** A JPEG with no `frames` row
(the entry-42 crash leak) is unreachable: nothing can ever read it, so deleting it
loses nothing. It runs in the background once at launch, which is exactly when new
orphans exist — the leak is created by a hard power loss, so the next start is when
there is something to collect.

**Automatic under disk pressure, stage-aware — eviction order.** Over cap,
`_evict_locked` reclaims non-motion frames first (oldest of those first) in
`collecting` and `running`, falling back to today's plain oldest-first once none
are left. In `tuning` the behaviour is **byte-for-byte today's**: plain ascending
id, motion and non-motion alike. Note what that does *not* mean — non-motion
frames are not spared in `tuning`, they age out at the same rate as everything
else; they are merely never *preferentially* targeted, because in that stage they
are the data the stage exists to collect.

The effect outside `tuning` is that the store keeps motion history — the
annotatable part — much further back for the same disk, and the leftover keep-all
window from a previous tuning stint is shed gradually as room is needed rather
than wiped on a mode toggle. Switching back to `tuning` before the disk fills
therefore still finds that window intact.

A second-order effect worth expecting: retaining motion history longer also grows
the annotation queue, since membership is "every undecided visit". That is not a
new problem — entry 233's *hide confident matches* filter exists for exactly this
— but this change makes it arrive sooner.

Because this strips windows *partially* (motion frames present, non-motion gone),
it must mark them. Today's eviction needs no marker: it removes whole windows
oldest-first, so a window is either fully present or fully absent and nothing can
misread it. Selective eviction breaks that, and an unmarked partially-stripped
window is the "empty danger set reads as safe" trap of entries 97/126/167 — a
scorecard would report near-perfect recall over frames that were deleted. The
stripped region is always a prefix (eviction works from the oldest end), so one
advancing `nonmotion_evicted_through` integer in the settings KV expresses it
exactly, and `motion_only_spans` folds it in as `[1, watermark]`.

Two constraints on that marker, both load-bearing:

- **`clear()` must reset it to 0.** The settings KV survives `clear()` while frame
  ids restart at 1, so a stale value would flag every brand-new frame as
  non-motion-stripped and banner a fresh store as unmeasurable. This is the
  rowid-reuse hazard `clear()` already deletes `mode_changes`, `purge_spans` and
  `label_flags` for (`store.py:1031`), and the one entries 141/143/144 each hit
  from a different direction.
- **It cannot be written with `set_setting` from inside `_evict_locked`.**
  `set_setting` acquires `self._lock` (`store.py:1062`) and `_evict_locked` runs
  with it already held — a plain non-reentrant `threading.Lock`, so that call
  deadlocks the store. It follows `_total_bytes`' shape instead: maintained in
  memory in lockstep during eviction, persisted through the connection the caller
  already holds.

**Never automatic.** The whole-window non-motion purge stays a manual job — it can
only ever delete a *previous* `tuning` window, since the stages that would want it
store no non-motion frames to begin with, so every frame it removes is retroactive
by construction. And nothing automatic touches `dataset_items`, crops,
`model_versions`, or `label_flags`.

### Migration and first launch

At launch the stage is read from the KV. When the key is absent it is derived
**once** and persisted:

- `motion_only` unset or `"0"` → `tuning`
- `motion_only == "1"` → `collecting`

`running` is never derived: it and `collecting` share a configuration, so nothing
in the store distinguishes them — it is a claim the household makes, not a state
that can be inferred. A fresh store with no keys at all lands on `tuning`, which
is both the honest cold start and the conservative one (you cannot recover frames
you did not store).

`restore` for both workers then becomes unconditional on a live app, since every
stage runs both. The `yolo_oracle` / `live_identify` keys are left in place but
unread — harmless, and cheaper than a KV migration.

**What this does to a box already in `tuning`** (where the real one is at the time
of writing): almost nothing. Capture stays keep-all, and eviction under `tuning`
is byte-for-byte today's. The single behavioural change is that unconditional
`restore`: an operator who had "YOLO all" switched *off* will find the detection
worker running after the update, sweeping full coverage continuously. That is the
coverage `tuning` wants — the point is that it stops being a switch one can forget
— but it is a GPU-load change on restart, not a silent no-op, so it belongs in the
changelog entry rather than being discovered from a fan curve.

### UI

The Start page's stage card replaces three controls: the capture-mode segment
(`index.html:970`), the "YOLO all" checkbox (`:979`), and the live-naming button
(`:994`). The "Workflow phases" list stops being documentation and becomes the
picker itself, each option stating what it does to capture and what it costs.
`/api/stats` gains `stage` alongside the existing `motion_only` /
`yolo_oracle` / `live_identify` snapshots, which stay as readouts.

In `running`, the page leads with health rather than controls — is the collector
up, is naming current, is the lamp holding (entry 274) — because that stage's
whole claim is that nobody is watching.

## Alternatives considered

- **No new state; derive from `motion_only` + `active_model()`.** Those two
  facts genuinely determine every knob, and `running` needs nothing beyond them.
  Rejected because the stage-2 hole exists precisely *because* the stages are
  inferred rather than stated — and because the household asked for a switch that
  says "we are done", which an inference cannot provide.
- **Stage as a preset over the existing switches.** A picker that applies a
  known-good combination and labels the current one, or "custom". Cheapest and
  fully reversible, but "custom" stays representable — so the combination that
  produced the hole is still reachable, and nothing can be enforced against it.
- **Fix the floor by decoupling live-identify's detect from its identify.**
  Detect always, identify only with a gallery. Attractive until the watermark:
  one watermark serving two coverage meanings means spans detected before a
  promotion never get identified. Superseded by running the detection worker in
  every stage, which needs no restructuring at all.
- **Auto-purging the non-motion window on entering `collecting`.** The obvious
  reading of "auto cleanup", and rejected: it makes a mode toggle irreversibly
  destructive, and a premature switch would eat a tuning window that switching
  back cannot restore. Disk pressure is the safer trigger — it reclaims the same
  frames, just later and only when the space is actually needed.
- **Giving `running` its own retention, to make it a real configuration.** Two
  candidates were weighed and both declined: an event-scoped purge of *labelled*
  visits' source frames (their crops are durable, so the frames are disposable —
  already listed as deferred on the Start page), and a time-based purge of motion
  frames older than N days. Preferential eviction already reclaims without either,
  and a stage that silently deletes more than its neighbours is precisely the
  surprise this design exists to prevent. Left deferred rather than dismissed:
  the event-scoped purge is the right lever if the byte cap ever proves too blunt.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** The `stage` setting threads through one chain —
  `store.py` (KV + eviction + `motion_only_spans`) → `yolo_oracle.py` →
  `app.py` → `admin-next/index.html` — and the two plausible streams (eviction
  policy vs. stage plumbing) both land in `store.py` and `app.py`, so splitting
  buys conflicts rather than parallelism.
- **The eviction change is the piece that earns adversarial verification** at the
  commit-gate review: it is the shared counter/accounting path
  (`_delete_frame_locked`, `_count` / `_motion_count` / `_total_bytes` in
  lockstep), it deletes frames irreversibly, and a missing
  `nonmotion_evicted_through` update turns a stripped window into a false
  ~100% recall. Per the root `CLAUDE.md` that surface gets its own finder.

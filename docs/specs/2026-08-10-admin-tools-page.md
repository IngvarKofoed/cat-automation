# A Tools page: run the crop re-cut from the workbench

Pick `letterbox + 10% margin` on the Model page today and both Validate and Build refuse
with *"No crops are cut at geometry 'letterbox+m10' yet — re-cut them
(compute.tools.recut_crops) or validate at the current one."* That message names a CLI, so
the only way past it is an SSH session on the compute PC — a dead end in the middle of the
one workflow the workbench exists to drive. This adds an admin page, **Tools**, whose first
card shows what crop conventions the labelled set actually holds, what a move to a chosen
target would do, and runs it as a cancellable background job — keeping the crops it supersedes,
so the move can be walked back, and moving them without a source frame wherever the target's
pixels are already on disk.

Tools also becomes the maintenance home: the non-motion purge, the orphan sweep, and
*Clear all frames* move off Start, which narrows to operating stage and collection.

## Key decisions

- **A third `CleanupManager` kind, not a new manager** (extends). `start_recut` joins
  `start_nonmotion` / `start_orphan` in `compute/collection/cleanup.py` — already the
  torch-free, single-job, batched-with-cancel manager, exercised end to end against a real
  temp store in `test_cleanup.py`. What the shared slot buys is **one status / cancel / poll
  surface** for every maintenance job, and one place the page's "a job is running" rule is
  enforced. It is explicitly *not* justified by preventing a purge from deleting frames under
  a running re-cut: that collision is already benign — `crops.materialize` returns False, the
  row is counted `failed` and left byte-for-byte untouched (`recut_crops.py:250-256`, tested
  at `test_crop_geometry.py:573`). A standalone `RecutManager` would duplicate ~120 lines of
  thread/lock/status machinery for none of that.
- **Re-cut and the training queue lock each other out** (new). `TrainingManager` is a separate
  slot, so nothing above stops a Build or Validate from embedding the labelled set while a
  re-cut moves it. That is the collision that actually bites, and it is silent:
  `_embed_items` skips a **missing** file in complete silence (`embed.py:410-412` — the
  warning at `:415` fires only for an *undecodable* one), so a gallery quietly enrols fewer
  crops than it selected, with nothing in the log. `start_recut` refuses while the training
  queue is busy, and the Build/Validate enqueues refuse while a re-cut runs — both as the 409
  the cleanup endpoint already returns.
  The lockout is **advisory**, not airtight: the two managers share no lock, so a Build
  enqueued between `start_recut`'s check and its start slips through, and the CLI bypasses it
  entirely. Left that way deliberately — a cross-manager lock is real complexity, and with
  superseded crops kept (below) the residual harm is bounded to a build reading a partial but
  pixel-correct set, since the row write is a compare-and-swap and nothing deletes files
  under it any more. Stated so a builder does not go hunting for the airtight version.
- **`recut_crops` is called, not copied** (reuses). `read_rows` and `recut`
  (`compute/tools/recut_crops.py`) already take their store write injected (`write_moves`),
  and the existing tests drive `recut` end to end against a real temp store
  (`test_crop_geometry.py:526-701`). The manager supplies `Store.update_dataset_geometry`
  exactly as `main()` does, so the CLI and the button run byte-identical logic — including
  the file/row ordering the module's docstring states as a safety property (cut to temp →
  `os.replace` → move rows → only then release the superseded file), which this change must
  not weaken.
- **Deleting a label must delete EVERY geometry variant of its crop** (breaking). This is the
  invariant `relink` rests on, and keeping superseded files is what breaks it. `relink` trusts
  that a file at the target path is this row's crop there — true only while files do not
  outlive the row that produced them. `_delete_crop_files` (`app.py:2635-2654`) removes just
  the row's *current* `crop_path`, which was sufficient when a re-cut unlinked the file it
  superseded: one row, one file. It no longer is. The failure is silent and exactly the class
  this repo fears: label a visit, re-cut to `letterbox+m10`, flip back to legacy, then
  **relabel** — `/api/label/relabel` (`app.py:2679`) deletes the row and its legacy file,
  leaves the `letterbox+m10` file orphaned, and `_commit_label` cuts a fresh crop at
  *today's* bbox. A later hop to `letterbox+m10` relinks the orphan: a row whose `bbox`
  column, whose stamp, and whose pixels disagree, embedded into a gallery with no symptom
  ever. The bbox genuinely moves — a `reanalyze` sweep overwrites `yolo-serial` verdicts, and
  entry 238 changed the class set.
  So the delete paths remove every variant: `cat_<id>/<frame>_<recv>.jpg` **and**
  `cat_<id>/*/<frame>_<recv>.jpg`. Enumerable by glob, needing no list of known conventions —
  which matters, since a foreign build's stamp can name a directory this one cannot parse.
- **The compare-and-swap gains `bbox`** (extends). `update_dataset_geometry` swaps on
  `(id, crop_path)` so a row replaced mid-run fails to match — which holds only while the
  replacement lands at a *different* path. A relabel to the **same cat** re-commits at the
  identical legacy path, so the swap matches a row this run never read and stamps it with
  pixels cut from the previous bbox: the silent disagreement between stamp, `bbox` column and
  actual pixels that the variant-deletion bullet above exists to prevent, reached by a race
  rather than a sequence. Narrow — a relabel of the very visit being moved, inside a run that
  lasts minutes — and latent in the CLI today, but this change makes the run a button and the
  training-queue lockout does not cover annotation.
  `bbox` is the exact predicate: it is what has to be unchanged for the pixels to still be
  right, so a relabel that moved the box fails the swap and one that did not is harmless.
  `read_rows` must therefore carry the **raw** `bbox` text alongside the floats it parses —
  the swap compares the stored string, not a re-rendered one.
- **The superseded crop file is KEPT, not deleted — for the CLI too** (breaking). Today
  `recut` unlinks the old crop once its row has moved (`_remove_old_crop`,
  `recut_crops.py:281-283`), which is what makes a migration progressively one-way: the trip
  back needs a live source frame, and frames evict continuously. Keeping the file makes the
  flip fully reversible for a few hundred MB, because legacy and non-legacy crops never share
  a path (`crop_rel_path`, `recut_crops.py:117-119`) and nothing reaps `dataset/` — the orphan
  sweep walks the frames media dir only (`cleanup.py:20-22`). The CLI changes with it rather
  than the API passing a flag: two behaviours behind one function is exactly how the button
  and the command stop being byte-identical, which the bullet above depends on.
- **Cancel arrives through the progress callback's return, on the repo's falsy convention**
  (extends). `recut`'s loop has no cancellation hook at all today; a re-cut decodes one
  source frame per crop, so a ten-thousand-crop run is minutes and must be stoppable.
  `on_progress` adopts the *signalling* half of the contract `embed_paths` defines and
  `TrainingManager` / `LiveIdentifyManager` already produce: **a falsy return stops**
  (`embed.py:392`, `if allow_cancel and not cont`; producers return `not stop_event.is_set()`).
  It does **not** adopt that contract's stop *mechanism*. `embed_paths` raises
  `EmbedCancelled`, discarding the run's return value — which here would throw away the
  partial summary the card is required to render and let the manager's generic handler record
  a cancel as an *error* with no counts. `recut` instead breaks out of its batch loop and
  **returns the summary it has**, matching `CleanupManager`'s own sibling (`_run_nonmotion`
  returns `canceled` alongside its counts). Signal borrowed from one, stop borrowed from the
  other, stated because a builder transcribing the named precedent would build the wrong one.
  That makes the CLI's own `progress` (`recut_crops.py:381`) load-bearing — it currently
  returns `None`, which is falsy, so it **must** be changed to `return True` or the CLI
  stops after its first batch and prints `re-cut 200, failed 0` as a clean success. The
  alternative — special-casing `cont is False` here — buys a second cancel idiom in a
  codebase that has one, so the CLI changes instead.
  Granularity: `on_progress` fires once per 200-row batch, *after* that batch's writes, so
  a Cancel waits out up to ~200 decode+cut+encode cycles.
- **The plan is its own read-only endpoint** (new). `GET /api/recut/plan?geometry=…` rather
  than a third `kind` on `/api/cleanup/estimate`, because that endpoint answers "how much
  would this purge reclaim" in `{count, bytes}` and a re-cut plan is a census plus a
  breakdown of what cannot move and why. Folding a third, unrelated shape into it would make
  the response type depend on a query parameter for no gain.
- **The plan read runs on its own short-lived connection** (reuses). A new
  `Store.recut_plan(target)` wraps the read the way `lighting_histogram` / `tuning_calendar` /
  `count_unidentified` do — the collector writes continuously through the shared write-locked
  connection, and this walks every `dataset_items` row with an `os.path.isfile` per row.
  Putting it there also keeps `_db_path` / `_media_root` private (`store.py:499-500`), which
  the endpoint and the manager would otherwise both have to reach past. It imports
  `recut_crops` **inside the method**: that module lazily imports `Store` at
  `recut_crops.py:385` precisely to keep the dependency one-way, and a module-scope import
  here would invert it into a cycle.
- **Housekeeping moves; Start narrows** (extends). The Cleanup card moves whole, and
  *Clear all frames…* leaves the Collection card — a destructive control sitting beside the
  everyday Start/Stop button is a slip waiting to happen. Start's route subtitle drops
  "and housekeeping", and its `stagePurgeHint` becomes a real cross-page `<a href="#tools">`
  instead of pointing "below". This half is **separable** — the re-cut card needs no page
  move, and the move needs no re-cut — and it is the larger share of the UI risk here, so it
  is the half to drop if this has to be cut down.
- **Cutting is the last resort, not the only path** (new). A move needs a source frame only
  when it needs *new pixels*, and often it doesn't. `recut`'s per-row step becomes a
  three-way choice, cheapest first:
  - **relink** — a file already sits at the target's path, so it *is* this row's crop at that
    geometry. Move the stamp; touch no pixels at all.
  - **copy** — some *other* kept file of this row carries the target's margin, so the pixels
    are right and only the path is wrong. A geometry is `(letterbox, margin)` and only
    `margin` reaches the stored pixels — `letterbox` is a resize applied at embed time
    (`_letterbox_square`, `embed.py:122`) — so any kept file at an equal margin will do, not
    merely the row's current one. Copy it, move the stamp.
  - **recut** — no kept file carries the target's margin. Decode the frame, expand the box,
    re-encode. The only branch needing a bbox and a live frame, and so the only one eviction
    can block.

  Sourcing `copy` from *any* margin-equal kept file, not just the current one, is what makes
  the arm loop lossless in every hop order. Parked at `letterbox+m10` and hopping to
  `letterbox` for the first time, the row's own margin is 10 and the target's is 0 — but the
  kept legacy file is margin-0 and correct, so the row moves without a frame. Comparing only
  the current stamp would send every such row down `recut`, where eviction blocks it, on a
  hop the comparison workflow produces routinely.

  A row stamp `parse_geometry` cannot read is treated as **unknown margin**, never as a
  match: `canonical_geometry` deliberately passes a foreign build's stamp through unchanged
  (`embed.py:104-119`), and feeding that to `parse_geometry` raises (`embed.py:79-100`). It
  must be caught and routed to `recut`, not allowed to fail the plan.

  Two payoffs. A `letterbox`-only flip becomes lossless — today it strands every crop whose
  frame has aged out, for a change that alters no pixel, which is changelog 440's finding.
  And with superseded crops kept (above), **returning** to an arm already cut is free
  forever, long after its source frames have evicted — which is what makes comparing three
  geometries a sequence of quick hops rather than a full re-cut per switch.
  That matters because `_expand_box`'s own docstring (`crops.py:74-77`) expects a margin to
  be *weakly negative* at this door ("extra context is common-mode and dilutes between-cat
  separation"), so the arms have to be measured against each other, not adopted on faith.
  This fixes entry 440's damaging *symptom*, not the redesign it asks for (see Non-goals): a
  store still holds one file per convention per crop, now deliberately, as the undo. Whoever
  takes that redesign inherits three callers of `crop_rel_path`, not one.

## Goals

- Make the Build/Validate pre-check's geometry blocker resolvable from the browser — which
  includes retiring the four places that currently instruct the operator to run the CLI (see
  *Design → The messages that name the CLI*).
- Show what conventions the labelled set holds *now* — the question behind "why is Build
  refusing", which no screen currently answers.
- Let `letterbox`, `letterbox+m10` and `letterbox+m25` be **measured against each other** and
  against legacy — the reason the blocker was hit at all — without a full re-cut per switch,
  and without putting the promoted gallery or the labels at risk.
- Make a `letterbox`-only flip **lossless** — no crop stranded by eviction for a change that
  moves no pixel — since that is the arm most likely to improve recognition.
- Give the destructive maintenance jobs one home, away from the daily-loop pages.

## Non-goals

- The pre-check's *exclusion* clause. Re-including a cat is already the Model page's
  checkbox list; nothing about it belongs here.
- The geometry-stamped-into-the-path redesign entry 440 asks for. Its worst *consequence* —
  stranding crops on a flip that changes no pixels — is fixed here; the redesign is not.
- A generic maintenance-job console (a registry with one shared plan/run/status UI). Three
  jobs do not pay for that machinery.
- Any change to how a gallery *selects* a convention. Build still takes exactly one.
- The CLI's `--limit N` ("a cautious first pass on a live store"). The plan states the size
  before the click and cancel stops it mid-run, so the control's only distinct effect is a
  deliberately half-moved set — from which Build simply sees fewer crops.

## Design

### The plan

`Store.recut_plan(target)` opens its own connection, calls `recut_crops.read_rows`, and
returns what the card renders:

```
{ target: "letterbox+m10",
  census:   [{geometry: null, count: 9871}, {geometry: "letterbox", count: 12}],
  at_target: 0,
  at_target_missing: 0,
  movable:  {recut: 9743, copy: 0, relink: 0},
  blocked:  {frame_gone: 140, no_box: 0, crop_missing: 0} }
```

`census` is every stored stamp, commonest first, and is target-independent — the part the
operator reads first. `canonical_geometry` returns an unrecognised stamp unchanged, so a
convention written by another build appears in the census as itself rather than being folded
into legacy; the target picker still offers only the fixed four (below).

`geometry` is **optional here**, and absent means "census only" — the other three keys are
omitted rather than computed against a guessed target. That is what lets the page render the
census before a target is chosen, and it is the opposite of the run endpoint's reading of the
same absent parameter (a 400 there, see below). The asymmetry is deliberate: a read with no
target is a useful question, an irreversible write with no target is a mistake.

The same call also returns the annotated `rows` `read_rows` produced. The endpoint drops that
key; the manager is the only consumer, and it is what stops the run path from needing its own
connection and its own copy of the read.

**`movable` is split three ways because the branches cost wildly different things.** A
`recut` row decodes, expands and re-encodes — minutes across thousands. A `copy` is file I/O.
A `relink` is a single `UPDATE`. Only `recut` can be blocked by eviction, so on a return visit
to an arm already cut, `movable.recut` is **0** and the whole job is near-instant. One merged
number would hide exactly the distinction the operator is choosing between, so the card leads
with `recut` — that count is the wait.

For a **margin-0 target** (`letterbox`, or legacy) `blocked.frame_gone` should be 0 — every
row is born at legacy, which is margin-0, and nothing deletes a kept file — but *should*, not
*by construction*. Two exceptions are real: crops re-cut by the CLI **before** this change
had their legacy file unlinked, and the manual reclaim this spec recommends can remove a
whole convention's directory. So the count is reported, never assumed away.

`read_rows`'s single `recuttable` flag becomes a `move` field naming the branch
(`"relink"` / `"copy"` / `"recut"` / `None`) plus the blocking reason, since the caller now
routes each row rather than only counting it. Evaluated in that order:

- `os.path.isfile(crop_rel_path(…, target))` → **relink**. Checked first because it is both
  the cheapest and the most likely on any store that has visited this arm before.
- a kept file of this row exists whose geometry parses to the target's margin → **copy** from
  it (none readable → `crop_missing`). No bbox, no frame. The row's *current* file is only the
  first candidate; the search is over the row's own paths, and an unparseable stamp is skipped
  rather than raising.
- else → **recut**, needing a parseable bbox and a live source frame, exactly as today.

`at_target` gains a companion, `at_target_missing`: a row whose stamp says target but whose
file is absent. Reachable through the manual reclaim this spec recommends — delete the live
convention's directory by mistake and every row reads at-target with nothing behind it, so
`movable` totals zero, the button disables, and the card reads *done* while the next Build
silently under-enrols. The plan already stats a file per row, so counting these is nearly
free, and it is rendered as a warning rather than folded into `at_target`.

`copy` and `recut` feed the same batched write, so the ordering guarantee is unchanged: write
to `<dest>.recut-tmp`, `os.replace` into place, move the row, never touch the old file — a
crash leaves a harmless orphan, never a row naming a file that is not there. `relink` writes
no file at all, so it is only the row move, and is trivially safe.

The page plans on mount with **no target selected**, so the census is on screen without a
click. Re-planning on every target change is one full read; at the store's ~10k labelled rows
that is milliseconds, and the read connection keeps it off the collector's path. It re-plans
once more when the status poll sees a run finish — otherwise the pre-run census stays on
screen beside a re-enabled button, which reads as *nothing happened* and invites a second
click. The target select is disabled while a job runs (a re-plan for some other target would
put two targets on one card), and *Re-cut…* is disabled whenever `movable` totals zero.

### The job

`POST /api/cleanup/run {kind: "recut", geometry}` → `CleanupManager.start_recut`, which:

1. reads the plan through the same `Store.recut_plan` the card called, and re-cuts the `rows`
   it returns — a *second* read, seconds later, so a label committed in between changes the
   counts. The card's numbers are therefore a close forecast, not a promise; the summary the
   job reports is the authority on what moved,
2. sets `total` to `movable`'s three counts summed — one progress bar, since all three
   branches advance the same row list; the card names the split beside it, because 9743
   re-cuts and 9743 relinks are wildly different waits,
3. calls `recut_crops.recut(store.update_dataset_geometry, todo, target, store.dataset_root,
   on_progress=…)`, where the callback records progress **and returns
   `not stop_event.is_set()`** — the same producer shape `TrainingManager` and
   `LiveIdentifyManager` already use,
4. returns `recut`'s summary (`{recut, copied, relinked, failed, rows_updated,
   old_files_kept}`) plus `kind` and `canceled`, which lands in `status()["result"]`.

A cancel leaves the set split across two conventions. That is not a broken state — the stamp
is per crop precisely so a store can hold two at once (entry 429), and Build filters to one —
but the card must say which counts moved, or a half-finished run reads as a failed one.

`geometry` joins `CleanupRunRequest` as a recut-only field, exactly as `before_ts` is
nonmotion-only. It is validated through the existing `_resolve_geometry` helper
(`app.py:2810`), so an unparseable value is a 400 and `m10` / `m10.0` cannot become two
targets — the same canonicalisation Build and Validate already apply.

**But it is *required* for `kind="recut"`, unlike everywhere else it appears.** Build and
Validate read an absent `geometry` as legacy (`_resolve_geometry` returns `None`), which is
the right default for a *read*. Here the same reading turns a body that merely forgot the
field into an unrequested re-cut of the whole labelled set back to legacy — recoverable now
that the old crops are kept, but still minutes of grinding and every stamp moved.
The endpoint rejects an absent or empty `geometry` for this kind with a 400 rather than
resolving it.

**Which means legacy needs a spelling of its own, or it becomes the one unreachable target.**
Rejecting empty removes `""` — the value `#mGeom` uses for legacy — and `_resolve_geometry`
cannot supply a word instead: `parse_geometry("legacy")` raises on the unknown token, by
design. So an operator could hop onto every arm from the browser and never hop back, which
both the Goals and the workflow section require. The run endpoint therefore takes the CLI's
own sentinel, `legacy` (`_LEGACY_WORD`, `recut_crops.py:72`), translating it to `None`
*before* `_resolve_geometry` — one spelling across the CLI and the button, and still no way
to mean legacy by accident. The plan endpoint accepts it too, so a legacy plan can be
requested; absent there still means census-only.

Consequently the shared option constant carries **values only, not labels**. Tools spells
legacy `legacy` where `#mGeom` spells it `""`, and `#mGeom`'s label for it — "current
(squashed)" — is a claim about the store that a migration makes false, which is precisely
what the Tools picker must not repeat.

Also to change: `/api/cleanup/run`'s 400 text (`app.py:1378`) now lists three kinds, while
`/api/cleanup/estimate`'s (`app.py:1367`) still lists two — correct, since re-cut does not use
that endpoint, but the two strings no longer match and the estimate one should say why.

### Comparing geometry arms — the workflow this exists for

The point is not to adopt a geometry, it is to **measure three against each other**:
`letterbox`, `letterbox+m10`, `letterbox+m25`, against legacy.

**One arm is live at a time, and that is deliberate.** `idx_dataset_src` is UNIQUE on
`(src_frame_id, src_recv_ts)` (`store.py:719`), so there is one row per crop and a row carries
one geometry. Loosening it to hold several variants per crop is the obvious way to make the
arms coexist and is rejected: that index *is* the label dedup guard — enforced at the DB
rather than trusted to the caller (`store.py:615`) — and relaxing it to run an experiment
risks the one artifact this repo treats as precious.

So the loop is: re-cut → Validate → re-cut → Validate. What makes it bearable is `relink`:
only the *first* visit to an arm cuts anything, and every return is a row update. What makes
it meaningful is that a validation run stamps the geometry it scored (`runner.py:111-115`,
"geometry arms are only comparable if each row says which one it was"), so the runs table
compares arms that were never live at the same moment.

**The live door is untouched throughout.** Identification cuts its crops at the *model
version's* geometry (`_model_geometry`, `live_identify.py:253-270`), not at whatever
`dataset_items` currently says, so the promoted gallery keeps naming cats while the labelled
set is being moved around underneath it.

What *is* unavailable while parked on an arm: a Build or Validate at any other geometry reads
zero crops. Nothing is lost — the files are all still there — but the experiment should end by
re-cutting to whichever arm is to be lived at, before a gallery is rebuilt.

**Known limit, inherited (entry 441):** the label route writes new crops at **legacy**, so a
store parked on an arm re-splits as annotation continues. The remedy is operational and cheap
now — re-run the re-cut before each Validate, where everything already moved `relink`s for
free and only the newly-labelled crops are actually cut. Changing the write path to follow a
configured default geometry is the real fix and is not in this change.

### Reversibility, and what it costs

Left as the CLI has it, re-cutting would be progressively one-way: a crop can move *back*
only while its source frame is live, and frames evict continuously, so a set re-cut today
might be unable to return next week. That is a poor trade for a decision the operator has no
way to test first — Validate can only score crops **already cut** at the target geometry
(`index.html:3868`), so there is no way to evaluate `letterbox+m10` without committing the
whole labelled set to it.

Keeping the superseded file removes the trap. A flip back then re-reads a file that is still
on disk and needs no source frame, so it works for 100% of crops rather than decaying with
eviction, and a re-cut becomes a decision the operator can walk out of.

What it costs, stated plainly because nothing here is automatic:

- Each geometry arm holds a full extra copy of the labelled crops — order a few hundred MB at
  ~10k crops. `dataset/` sits **outside** the frame store's byte cap and is never evicted
  (that is the point of it), so this grows with each arm and is bounded only by how many the
  operator tries. On a **copy** arm the two files are byte-identical, which is the
  clearest case there will ever be for the deferred reclaim job. Plain copy, not a hardlink:
  saving those bytes would make two rows share one inode for a gain this spec has already
  decided it can afford.
- **Nothing reclaims them.** There is no reaper and this spec adds none — consistent with
  ARCHITECTURE's "cleanup otherwise stays manual", and with the orphan sweep deliberately not
  walking this tree. Reclaiming means deleting the stale convention's subdirectory by hand.
  A "drop superseded crops" job is the obvious follow-up and is left out here.

`recut`'s summary loses `old_files_removed` (always 0 now) for `old_files_kept`; a counter
that can only report zero is worse than absent on a card.

The confirm dialog says what is true: *the labels are safe, the crops at the old geometry are
kept so this can be reversed, and the disk cost is another full copy of the labelled set.*

### The page

Route `tools`, mounted last in `ROUTES` — maintenance is not part of the daily loop.
Three cards:

1. **Re-cut labelled crops** — the census table, the target select, the plan readout, a
   danger-styled *Re-cut…*, and the job readout (progress / cancel / result).
2. **Cleanup** — the non-motion purge and orphan sweep, moved verbatim.
3. **Clear all frames** — moved out of Start's Collection card, with its existing confirm.

Start's three-second `refresh` stays — it also drives the stage picker, the purge hint and the
store stats off `/api/stats` — but drops its `/api/cleanup/status` fetch along with the cards
that consumed it, and gains no replacement readout. The purge hint already links here, and a
line reporting a running job would put that fetch straight back.

The target select carries the same four options as the Model page's `#mGeom`
(`index.html:3858`), from a module-level constant both mounts read. The no-shared-JS rule
(entry 80) is between `/admin` and `/`; within the one admin file a shared constant is what
stops two spellings of one convention drifting apart. It blurs on change, like every other
select on this console (entries 235/242/298).

**It does not inherit `#mGeom`'s default.** That select preselects
`<option value="">current (squashed)</option>` — legacy — which is a sound default for a
*read* and a trap for this button: once a store has migrated to `letterbox+m10`, mounting
Tools would show "9743 re-cuttable" beside a red *Re-cut…* whose single click reverts the
migration, under an option labelled "current" that the migration made untrue. So Tools leads
with an unselected placeholder, and *Re-cut…* stays disabled until a target is chosen. The
confirm names the target and the count.

**Wiring, not just markup.** The existing click handler binds
`view.querySelectorAll('button[data-kind]')` (`index.html:1146`), so giving the re-cut button
a `data-kind` would drop it into that handler and produce a *"Sweep orphaned files?"* confirm
followed by a `POST` with no geometry — the exact silent-legacy-re-cut the paragraph above
rejects. So the re-cut button gets its own listener and carries `data-act="run"` **without**
a `data-kind` — enough for the disable rule below to reach it, not enough to be claimed by
the purge handler.

**One slot, three cards.** Every run button disables while *any* job runs — `renderCleanup`
already does this across `[data-act="run"]` — so the page states it, or *Drop* greying out
during a re-cut reads as broken.

**One owner for the re-cut button's `disabled`.** That shared sweep assigns
`b.disabled = c.running` *unconditionally* (`index.html:1281`), both directions — so on every
idle 3-second poll it would re-enable a button the card had disabled for having no target or
nothing to move, handing the operator a live danger button and a confirm naming no target
(the 400 is a backstop, not a defence). The re-cut button is therefore excluded from that
selector and one place computes `running || !target || movable_total === 0` for it.

**Three default-to-X branches the move must fix.** Each assumes exactly two kinds and falls
through to one of them, so a third silently inherits another job's identity — entry 288's
class, where one job's outcome is reported as another's:

- `index.html:1282` and `:1295` — `renderCleanup` routes a finished summary with
  `c.kind === 'orphan' ? '#orJob' : '#nmJob'`, so a re-cut result would render "dropped N
  frames" into the purge card.
- `index.html:1148` — the click handler's `kind === 'nonmotion' ? '#nmJob' : '#orJob'`,
  defaulting the *other* way.
- `index.html:1156` — the confirm label, `kind === 'nonmotion' ? 'Drop…' : 'Sweep…'`.

All three become explicit maps keyed by kind, with an unknown kind rendering nowhere rather
than into whichever branch is last.

### The messages that name the CLI

The blocker in this spec's opening is one of four places that send the operator to a shell.
All four are rewritten to point at the Tools page; the page is not done while any of them
still reads as the only route:

- `app.py:2905` — the Validate pre-check, and `app.py:3050` — the Build pre-check.
- `gallery.py:237` — the different-geometry hint ("or build at the geometry they carry").
- `index.html:3869` — the `#mGeom` hint, which today prints the literal command
  `python -m compute.tools.recut_crops --to letterbox+m10 --apply`.

The API strings are consumed by both dashboards as plain text, so they name the page
("re-cut them from the admin Tools page") rather than carrying markup; the one in
`index.html` becomes a real link.

## Alternatives considered

- **A standalone `RecutManager` + `/api/recut/*`.** Honest naming — a re-cut is not cleanup —
  but it duplicates ~120 lines of thread/lock/status machinery, and splits the one status /
  cancel / poll surface the page renders three jobs through.
- **A third kind on `TrainingManager`.** Wrong home: that is the GPU/torch queue with
  dedup-by-params enqueue semantics, and a re-cut needs neither.
- **Shipping without the margin-unchanged branch**, on the grounds that `letterbox+m10` needs
  a real re-cut either way so the branch does not unblock the motivating target. Rejected once
  `_expand_box`'s docstring was read: the margin is expected to be *weakly negative* here, so
  `letterbox` alone is the arm most worth running — and without the branch that arm is the one
  that strands crops, for a change that alters no pixel.
- **Deleting the superseded crop, as the CLI does today.** No disk growth and one fewer thing
  to explain, but it leaves the re-cut one-way for no gain: a few hundred MB is cheap against
  a labelled set that took a human's attention to produce, and the operator cannot evaluate a
  geometry without first committing the whole set to it.
- **Adding a "drop superseded crops" job** in the same change, so the disk cost is reclaimable
  from the page. Deferred — it is a fourth job kind, and the duplicates are inert until
  someone wants the space back.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** The backend and the page are one thread, not two: the job's
  summary shape, the plan's payload and what the card renders are the same decision seen from
  three sides, and the housekeeping move is a cut-and-paste through the same `renderCleanup`
  the new kind rewrites. Splitting it would mean two agents negotiating one JSON shape.
- Two bullets need interpreting rather than transcribing, and they are why this stays off a
  cheaper tier: keeping the superseded crop `(breaking)` changes a documented safety ordering
  in `recut_crops.py`, and the tests at `test_crop_geometry.py:526-701` assert the old
  deleting behaviour; and the margin-unchanged branch `(new)` splits a predicate every one of
  those tests exercises.
- Both deserve tests that fail against the old behaviour before they pass against the new —
  entry 411's rule. Three that carry the design: a crop whose source frame is **gone** moves
  on a `letterbox`-only target (the whole claim); a **relabel between two visits to one arm**
  leaves no stale variant for `relink` to adopt (the silent-wrong-pixels case, and the one
  worth writing first); and `legacy` round-trips as a run target through the API. A fourth
  covers the swap: a same-cat relabel landing mid-batch must leave the new row untouched.

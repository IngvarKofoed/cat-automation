# Mark a visit for labelling from the user dashboard

A one-tap control in the user dashboard's playback modal that records a
persistent flag on the visit on screen — "this one needs a label / this name is
wrong" — plus a **Flagged** mode in the admin Annotation page to work the flagged
set later. The flag is a small span-keyed table; it never touches the labels the
visit already has, and the annotation queue's own read is untouched.

The gap it fills: today the only way to act on a wrong or unnamed visit is to
remember it and go hunting on the compute PC. The annotation queue can't carry
that intent — it is *virtual* (`Store.annotation_queue_page`,
`compute/collection/store.py:4466`: undecided = the ABSENCE of a `dataset_items`
row, over a recency-bounded tail scan), so there is nowhere to put "look at this
one".

## Key decisions

- **Span-keyed `label_flags` table** (new). One row per flagged event, keyed by
  the event's frame span (`start_id`, `end_id`) rather than per frame or per
  crop. That is what makes a `motion_only` / `unrecognized` event flaggable at
  all — it has no YOLO box, so it cannot appear in the annotation queue, yet it
  is exactly the kind of visit a user wants to flag. Being frame-id keyed it is
  dropped by `clear()` alongside `groups` / `mode_changes` / `purge_spans`
  (`store.py:946` — rowid reuse would otherwise re-point a stale span at
  unrelated new frames). Not cascaded by eviction; see *States*.
- **A flag never touches a label** (new). Flagging a named visit leaves its
  `dataset_items` rows alone, so the operator sees what it *was* called before
  deciding. The alternative (strip the label on flag, so the ordinary queue
  picks it up) destroys the context the correction needs.
- **Third mode in Annotation, not priority in the queue** (extends). Reuses the
  `#aMode` segmented control (`admin-next/index.html:2046`), `setMode`
  (`:2455`), the stage renderer and the digit keys — `Queue | Flagged |
  Labelled`. `annotation_queue_page` is not modified, so the active-learning
  worst-first ordering and its bounded scan keep their current behaviour.
- **The flagged unit is the span the user saw, not a re-cluster** (diverges).
  The queue clusters present frames with `_gap_split`; the flagged review labels
  exactly the frames inside the flagged span. So what the operator labels is
  what the user tapped, even if the surrounding motion has since grown into a
  wider cluster.
- **Every flagged decision goes through `POST /api/label/relabel`** (reuses).
  It is delete-then-commit (`app.py:2308`), a superset of `POST /api/label`:
  on an unlabelled span the delete is a no-op. One code path in the new mode,
  whether or not the span was already labelled. It deletes rows only for the
  frame ids sent, and the record's frames are resolved live, so a label whose
  source frame has since evicted is never touched — but see *Disclosure*.
- **A flagged span drops the detection-confidence floor** (diverges). The queue
  admits cat boxes only at or above `_ANNOTATE_MIN_CONF` (0.3) while YOLO runs
  recall-first at 0.15, so a faint 0.2 cat box — a real crop — is invisible to it.
  A flagged span takes cat boxes at ANY confidence. The floor exists to keep
  phantom empty-scene detections out of the *bulk* queue (entry 73); a human
  pointing at one specific visit is not that case. Still **cat-only**
  (`_best_box`), so the divergence is one threshold, not a class change.
- **A flag is identified by span OVERLAP, on both sides** (new). The read rule
  (an event is flagged when a flag overlaps its span) and the write rule are the
  same one, so `add_label_flag` dedups on overlap rather than on an exact
  `(start_id, end_id)` pair. An event's cluster grows as later motion lands
  within `_VISIT_GAP_MS`, so an exact-pair key would mint a second flag on a
  re-tap and leave un-mark with no defined target.
- **Resolving a flag is a hard delete of its row** (new). No `resolved_ts`
  history column — a flag is a work item, not precious output, and re-flagging
  the same visit is allowed. Keeps the table tiny and the "is this flagged"
  test a plain existence check.
- **Two reads, split by audience and cost** (new). `GET /api/label/flags`
  returns bare spans — what the phone needs for button state, cheap enough to
  fetch beside the feed. `GET /api/label/flagged` does the expensive resolve
  (frames, boxes, current label, state) and is admin-only.
- **`events()` is untouched** (reuses). The flag is *not* joined into the
  activity feed. `events()` (`store.py:3679`) is the hot path deliberately
  bounded in entries 102–105; the user page fetches the flag list separately and
  matches spans client-side.
- **`flagged_visits` resolves PER SPAN, on its own short-lived WAL connection**
  (reuses). Same connection discipline as `tuning_calendar` / `labeled_visits`
  (`store.py:4686`). Deliberately *not* the min..max range read
  `_attach_queue_distances` uses (`store.py:4634`): there the range is bounded by
  a recency-capped scan, whereas flags are never auto-pruned, so one three-week-old
  `gone` flag would turn every pass into a full-store range read.

## Goals

- From the phone, one tap marks the visit on screen for labelling, and the mark
  is still there tomorrow.
- The mark is visible in the feed, so the user can see what they already
  flagged without opening each event.
- On the compute PC, the flagged set is a short worked list: see the visit, see
  what it is currently called, label it (or dismiss), flag gone.
- A visit with no detection can still be flagged, and the flag says why it
  isn't labellable yet.

## Non-goals

- **Labelling from the phone.** The user marks; the operator labels. Cropping,
  quality grades and the roster stay in admin.
- **A reason or note on the flag.** One tap, no form. If "why" turns out to
  matter, it is an added column later.
- **Notifications** when something is flagged, and any flag count on the user
  dashboard.
- **Flagging from the Cats view or the admin Activity page.** One entry point.
- **Priority ordering** of flagged visits inside the ordinary queue.
- **Surviving `clear()`** — flags are frame-id-keyed work items, not labels.
- **Cropping a person- or bird-classed box.** A visit YOLO called a *person* has
  no cat box, so it stays `no_detection` → dismiss. Accepting `_best_detection_box`
  there would let a mis-classed resident be labelled, but it needs class-preference
  logic the cat-only path doesn't have; deferred until it actually bites.

## Design

### The flag

```sql
CREATE TABLE IF NOT EXISTS label_flags (
  id         INTEGER PRIMARY KEY,
  start_id   INTEGER NOT NULL,   -- frames.id lower bound (inclusive) of the flagged event
  end_id     INTEGER NOT NULL,   -- frames.id upper bound (inclusive)
  start_ts   INTEGER NOT NULL,   -- recv_ts of the span's first live frame, captured at flag time
  created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_label_flags_span ON label_flags(start_id, end_id);
```

The index is deliberately **not** UNIQUE: identity is span overlap, which no
column constraint expresses (see *Idempotency*).

Added to the schema script (`store.py:496`) and to the `clear()` cascade
(`store.py:946`) with the same rowid-reuse comment `purge_spans` carries.
`start_ts` is captured at flag time so the admin list can still name *when* a
flag was for after its frames age out.

`POST /api/label/flags` takes `{start_id, end_id}` — the event's own span, which
the client already has from `/api/events`. The route validates
`0 < start_id <= end_id`, then `Store.add_label_flag` resolves
`MIN(recv_ts)` over the live frames in the span:

- no live frames → **409**, `"those frames have aged out"`. This is also the
  span-exists check; a client can't invent a flag for ids that were never there.
- otherwise, under the store lock, an overlap probe
  (`WHERE end_id >= start_id AND start_id <= end_id`) returns any existing flag
  for this visit; only if there is none does it insert. Either way it returns the
  row, so a re-tap is idempotent even after the event's cluster has grown.

`GET /api/label/flags` → `{flags: [{id, start_id, end_id, start_ts,
created_ts}, ...]}`, newest first. `DELETE /api/label/flags/{flag_id}` removes
one — the admin's dismiss. The phone's un-mark posts the *event span* to
`POST /api/label/flags/unmark` instead, which deletes every flag overlapping it:
the user's gesture means "this visit is not marked", and with the id it happens
to hold it could otherwise clear one of two overlapping flags and leave the ⚑ on
with no way to turn it off.

### User side (`compute/api/web/user/index.html`)

`loadRecentActivity` fetches `/api/label/flags` alongside `/api/events` (it
already fetches `/api/cats/overview` in parallel) into a module-level array. An
event counts as flagged when any flag's `[start_id, end_id]` **overlaps** its
own span — not an equality test, because an event's cluster can grow as later
motion lands within `_VISIT_GAP_MS`, so the span a flag was made against can be
a subset of today's.

- **Feed card** (`eventRow`, `:1138`): a small ⚑ glyph beside the identity chip
  when flagged. No new fetch — the list is already loaded — and without it the
  user has to open every event to see what they already marked.
- **Modal** (`openEvent`, `:1306`): a button in the dialog, on the
  `#playerStats` band below the filmstrip (`:786`), reading **"Mark for
  labelling"** / **"Marked for labelling ✓"**. Tapping toggles: POST to flag,
  POST `unmark` to clear. Optimistic — the label flips at once and reverts with an
  inline message if the call fails, matching how the rest of the page treats a
  failed write.

The 409 case renders as *"these frames have aged out — nothing left to label"*,
which is the truth: without frames there is no crop to make.

### Admin Flagged mode (`compute/api/web/admin-next/index.html`)

`GET /api/label/flagged` returns one record per flag, newest first, in the shape
the Annotation stage already renders (`frames`, `rep_frame_id`, `peak_area`,
`peak_score`, `span`, `start_id`, `end_id` — as `annotation_queue_page` returns)
plus:

- `flag_id`, `created_ts`
- `state`: `labellable` | `partial` | `no_detection` | `unswept` | `gone`
- `coverage`: `{n_live, n_swept, n_boxed}` — the counts the state is derived from.
  Deliberately not an `n_span`: the id width (`end_id - start_id + 1`) is not a frame
  count once anything in the window has evicted or been purged, so reporting it beside
  two real counts would read as one.
- `current_label`: `{label_kind, cat_id, cat_name, n_frames, mixed}` or `null`

`Store.flagged_visits` resolves each flag independently — three small
id-range-indexed reads per span (live frames; `yolo-serial` verdicts, boxed via
`_best_box` at `store.py:4207` but **unfloored**, unlike `_present_frames` at
`store.py:4290`; `dataset_items` rows) — on one short-lived read connection. Per
span rather than one min..max pass over all of them: see the Key decisions.

Because the floor is dropped here, a frame's confidence is worth showing: the
rep crop's own score already renders beside the visit peak on the Annotation
stage (entry 156), which is what tells the operator a 0.19 box is a faint one.
Crop quality still seeds from score and area ratio via the existing `seedQuality`
(`index.html:2092`), so a faint box grades `poor` on its own and is kept out of a
gallery build by the grade filter — the floor's protective job, done where it
belongs.

The mode reuses the existing stage (rep crop + full frame with box), the roster
digit legend, and the keys, with the flag's current label shown as a chip:

- `1–9` / `u` / `x` → `POST /api/label/relabel` over the span's boxed frames, then
  `DELETE` the flag — in that order, so a failed write leaves the visit flagged
  rather than dropping the work from both lists.
- `d` → dismiss: delete the flag only, leaving any existing label untouched.
- `n` / `p` → next / previous.

No `g` (ignore) here, unlike the queue: ignore is the queue's "skip without judging",
and dismiss already plays that role in Flagged review — without rewriting the labels
the span may already carry.

Writes here are **synchronous** and `busy`-guarded, like Labelled review — not
optimistic like the queue (entry 216). The flagged set is a handful of visits
worked deliberately, so there is no keypress-lag problem to solve, and a
synchronous write keeps "flag gone" honest.

The mode button carries the count (`Flagged (3)`) from `GET /api/label/flags` —
the tiny-table read, not the resolve — so mounting Annotation in Queue mode never
pays for a flagged resolve nobody is looking at.

#### Disclosure

Two things the operator must not have to infer, both already house patterns:

- **`mixed`.** An event span is a *motion* cluster, so `_gap_split` over
  *detected* frames can have split it into two annotation visits with different
  labels (a detection hole inside one motion run; or two cats at the door).
  `labeled_visits` already computes this (`store.py:4744`) and the Labelled stage
  renders a `mixed` tag with a tooltip (`index.html:2333`) — the flagged row
  carries and shows the same, because one keypress rewrites both as one identity.
- **"recorded N of M".** `add_dataset_items` skips a frame that evicted between
  the resolve and the write, so `inserted` can come back short. Echo the Labelled
  path's message (`index.html:2386`) rather than let a freshly-drawn chip claim a
  label that did not fully persist.

### States

State is derived from **coverage** — `n_swept` vs `n_live` — never from the mere
existence of an `analysis` row:

Derived in this order, so the five are total and mutually exclusive; labelling is
offered whenever `n_boxed > 0`, and **Analyse** whenever coverage is incomplete:

| `state` | Derivation | What the row offers |
|---|---|---|
| `gone` | `n_live == 0` | Dismiss only; shows the flag's `start_ts` |
| `unswept` | `n_swept == 0` | **Analyse**, or dismiss |
| `partial` | `0 < n_swept < n_live` | **Analyse**, plus labelling if `n_boxed > 0` |
| `labellable` | fully swept, `n_boxed > 0` | Full labelling |
| `no_detection` | fully swept, `n_boxed == 0` | Dismiss (nothing to crop) |

Note `partial` outranks `labellable`: a span with boxes AND unswept frames is still
labellable, but it is the incomplete coverage the operator needs to see, since a cat
may sit in a frame the detector never looked at.

Partial coverage is the normal case, not an edge: the live-identify worker writes
`yolo-serial` verdicts only *inside* visit spans (entry 76), and the oracle worker
is forward-only, fills only missing verdicts, and idles under motion-only capture
(entries 142, 149). A span with one swept frame and forty unswept ones must not
read as `no_detection` — that would tell the operator the detector had rejected
frames it never looked at, inverting the console's own honesty rule (entries 113,
157: unmeasured must never present as measured), on the page whose job is
explaining why a visit isn't labellable yet.

**Analyse** posts `/api/analysis/run` with `yolo-serial`, `reanalyze: false`,
`motion_only: true`, scoped to `[start_id, end_id]` — entry 91's tight sweep,
the same shape the Activity page's per-event re-analyse uses. It is offered
whenever coverage is incomplete (`unswept`, `partial`), so the operator can make
the visit labellable without leaving the page; progress shows on Sweeps as any
other job does. A fully-swept `no_detection` span gets no sweep control: YOLO has
already rejected every frame there, and offering a re-run would be a lie.

### Idempotency and edge cases

- Re-flagging is a no-op: the overlap probe finds the existing flag and returns
  it. This holds even after the event's cluster has grown, which is why identity
  is overlap and not the exact id pair.
- Two flags can still overlap when two separately-flagged events later merge into
  one cluster (each was non-overlapping when made). Then the merged event's
  ⚑ reflects both, un-mark clears both, and the Flagged list shows two rows for
  sub-spans of one visit. Rare, and each row is independently labellable — a known
  limit, not worth a merge pass.
- Labelling a flagged visit from the *ordinary* queue leaves its flag standing.
  The Flagged row then shows the new `current_label`, and `d` dismisses it. This
  is why dismiss exists as a distinct action rather than being implied by any
  label write.
- A flag whose frames have aged out is **never auto-pruned** — it stays as
  `gone` until the operator dismisses it. A flag that silently disappears hides
  the fact that the work was lost to retention, which is the "absence of
  evidence reads as safe" trap of entries 97 / 126 / 167. The cost is a list
  that can accumulate dead rows, which one keypress each clears.

## Alternatives considered

- **Flag as priority inside the existing queue** (strip the label on flag, sort
  flagged visits first). Rejected: the queue is undecided-only, so the current
  label — the context for judging the correction — would be destroyed at flag
  time; and the queue's read is a bounded DESC tail, so an older flagged span
  needs a second query merged in regardless, which is most of the cost of the
  separate mode without its clarity.
- **Requeue only, no new state** (`POST /api/label/delete` on the visit's
  frames). Rejected: a no-op for an unlabelled visit (already undecided), and a
  requeued older visit falls outside the queue's recency scan, so it never
  resurfaces — the tap would appear to work and do nothing.
- **A `flagged` `label_kind` in `dataset_items`.** Rejected outright: a
  `dataset_items` row *is* the "decided" predicate, so it would remove the visit
  from the queue — the opposite of the intent — and collide with the existing
  row on a already-labelled frame (`idx_dataset_src` is UNIQUE).
- **Keeping the 0.3 floor on flagged spans.** Rejected: it left a flagged visit
  whose only box is a faint cat detection dismissible-only, which is the visit the
  tap exists for. The floor's protective job is delegated to the quality grade,
  where a faint crop already grades `poor` and stays out of a gallery build.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Four files (`store.py`, `app.py`, and the two
  standalone dashboards), but one thread: both frontends consume the
  `flagged_visits` payload, so its exact shape has to settle before either can be
  written, and splitting them would mean two agents guessing at it.
- The two dashboards share no CSS or JS by convention (entry 80), so if the
  backend lands first they *could* be handed to parallel Sonnet 5 workers — the
  overlap-dedup and coverage-state logic is the part that wants Opus.
- Worth store tests of their own: overlap dedup (including after a span grows),
  the five-way state derivation, and that `clear()` drops the table.

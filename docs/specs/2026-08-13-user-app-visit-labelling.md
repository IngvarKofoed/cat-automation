# Confirm or dispute a visit from the phone

The household already steps through visits in the user app's playback modal hunting
mislabels. That pass supplies the scrutiny a trustworthy label needs, so it should be able
to *write* the label instead of only marking the visit for someone at a desk to redo. The
modal gets two buttons and no picker:

- **Yes** — the name on the card is right. It becomes the visit's label, in one tap.
- **Label later** — it's wrong, or the phone can't safely decide. This is the existing ⚑
  toggle, unchanged in behaviour: the visit goes to the desk, where the roster and the
  full-size player are.

So the phone never *chooses* a cat, it only agrees or defers. Yes writes through a new
span-keyed endpoint that resolves the span to its undecided detected frames server-side and
stamps the rows `source = 'user-confirm'`, keeping them distinguishable from desk labels
forever.

## Key decisions

- **Yes confirms, it never picks** (new). The written `cat_id` is the identity the card was
  already showing — there is no cat list, no *stranger*, no *not a cat* on the phone. Every
  case the confirmation can't express is exactly what the ⚑ is for, which is why the dispute
  half needs no new machinery: it is today's flag, untouched.
- **Span-keyed write endpoint** (extends). `POST /api/label/visit {start_id, end_id, cat_id}`,
  modelled on `POST /api/identify/visit` (`app.py:3418`) — the other per-visit action the
  phone calls. Both bounds REQUIRED (elsewhere an omitted bound means "whole store"),
  `_reject_bool` on each, `_validate_bounds` for ordering, `_MAX_VISIT_SPAN` (10k ids) width
  cap. Same reason as there: no-auth LAN, so a stray span must not become a bulk write.
- **`cat_id` is a concurrency check, not an instruction** (new). The server re-resolves the
  span's identity at write time and commits *that*; the client's `cat_id` is the name it
  displayed, and a mismatch is a **409** whose `detail` is a sentence naming what the span
  now reads as (the client re-probes rather than being handed a state blob — its error
  renderer reads `detail` as a string, and the reader has already been advanced past this
  visit, so a code they must interpret is no use). Load-bearing because the threshold is
  applied at READ time and restates all
  history when changed (entry 425) — so the name a phone is looking at can genuinely differ
  from the name the server would give the same span a minute later. "Yes" has to mean yes to
  what was on screen.
- **A contested span refuses Yes, measured from the BOXES** (new). Refused when some
  undecided frame carries two or more cat boxes at or above `_ANNOTATE_MIN_CONF` — two cats
  in shot together. Only one box per frame becomes a crop, so a one-tap label over such a
  span files whichever cat had the larger box each frame under a single name, and nothing
  downstream could notice. Tailgating is expected at this door (entry 319).

  **Deliberately not measured from the identity votes.** The first implementation contested
  whenever two cat names held a below-threshold match anywhere in the span, and that fires
  on ordinary single-cat visits: the active model declines almost nothing (entry 425), so a
  30-frame span routinely has a few frames whose nearest neighbour is a lookalike — Store
  Sultan ↔ Store Jihn is 57% of all errors (entry 422). That is frame-to-frame embedding
  noise, which `_aggregate_identity` already absorbs by taking a **majority** vote rather
  than requiring unanimity; treating dissent as a second cat made the guard fire on most
  real visits and claimed "more than one cat here" from a reading that never counted cats.
  Found on the real store, not in a fixture — the fixture encoded the wrong assumption.

  Accepted consequence: two cats that alternate without ever sharing a frame are not
  caught. A vote-share threshold would not reliably catch them either — at that point the
  shares are indistinguishable from lookalike noise — so the desk's Labelled review is the
  backstop rather than a tuned number on a fail-safe.
- **The server resolves span → frames** (new). A user-facing event clusters *motion* frames
  while an annotation visit clusters *detection-present, undecided* ones
  (`store.py:5312-5315`), so one span can hold zero, one, or several annotation visits. Every
  undecided present frame in the span takes the one identity — that is what the person
  tapping perceives as one visit. Grades stay per cluster: `seed_quality` is computed against
  each cluster's own `peak_area`, so the rows are graded exactly as admin would have graded
  them. The union is over *frames*, never over the peak. A span that already holds labels is
  still confirmable for its undecided remainder — spans grow (entry 224), so a part-labelled
  visit is routine.
- **`source = 'user-confirm'`** (extends). `dataset_items.source` exists, defaults to
  `'detector'`, and nothing reads it today (`store.py:715`), so a new value costs nothing and
  buys three things: auditable in admin, excludable from a gallery build, and A/B-able ("did
  the phone labels help or hurt?"). Provenance is the price of trusting a channel with less
  deliberation behind it.
- **Gated by a lazy probe, not by widening the feed** (diverges). `GET /api/label/visit`
  answers "can Yes act here, and what does this span already hold" when the modal opens. One
  visit is open at a time, so the 100-event feed pays nothing — and `events()` is the call
  this repo has twice had to claw back from per-span cost (entries 256, 265).
- **Optimistic serial write chain** (reuses). `_commit_label` decodes and writes one JPEG per
  visit frame in-request (`app.py:2747`, entry 297) — 1–2 s on a 100-frame visit. Yes advances
  immediately and the POST joins a serial chain, with admin's named amber pill ("Saving
  Mittens · 06:14…") reporting what is still in flight, per entries 294–296. A spinner per tap
  is what would make a 40-visit audit not worth doing.
- **A recorded Yes clears any overlapping ⚑** (reuses). Entry 227's rule, and it keeps the
  pair coherent — the two buttons are opposites, so confirming resolves a dispute raised
  earlier. The flag clears only when the write reports ≥ 1 row, because `inserted: 0` is the
  ordinary aged-out path and dropping the mark there would discard both the mark and the
  decision silently.

## Goals

- One tap turns a visit the audit has just confirmed into a label, without leaving the modal
  or interrupting the walk through visits.
- Keep phone-written labels distinguishable from desk-written ones, permanently.
- Leave every case the phone can't safely decide reachable from the same footer, via the ⚑.

## Non-goals

- **A cat picker on the phone**, in any form — including *stranger* and *not a cat*. This is
  the deliberate scope line: the phone confirms or disputes, the desk decides.
- **Re-labelling an existing label from the phone.** The probe reports what a span already
  holds, read-only; the ⚑ is the recourse, and changing a stored label stays admin's Labelled
  mode (`requeue` / `relabel`, entries 241–245).
- **Surfacing the model's guess in admin's annotation queue.** A real gap —
  `annotation_queue` returns `distance`/`uncertain` but no `cat_id`/`cat_name`
  (`store.py:5490-5515`), and admin renders only `nearest d=…`
  (`admin-next/index.html:2832`) — but a separable one, with its own spec.
- Auth, on this or any route (the prototype's standing trust model).
- Any change to `label_flags`' shape, ⚑ semantics, or the annotation queue's filters.

## Design

### The two buttons

The modal footer today holds the ⚑ toggle, the conditional "Analyse this visit" button, a
shared `aria-live` readout, and the visit nav (`user/index.html:966-983`). The confirm button
joins the ⚑ on its existing line, so the footer gains no row:

**`✓ Yes`** appears only when the probe says all three hold: the span has undecided detected
frames, it carries a *named* identity (`cat_id` non-null — an "unknown cat" aggregate has
nothing to confirm), and no undecided frame holds two cat boxes. Absent any of the three it
is not rendered
— never a disabled button, never a silent no-op (entry 283; and entry 287's reminder that a
`.hidden` toggle on an element with no qualified rule does nothing at all, so visibility gets
verified by computed style).

A bare *Yes* works only because that rendering rule makes the pairing structural: the button
exists **iff** the identity chip directly above it in the same dialog carries a name, so the
question it answers is always on screen. Two places still carry the name explicitly, because
the button no longer does:

- **The accessible name** is the full assertion ("Yes, this visit is Mittens"), not the
  visible "Yes" — a screen reader reaching the button gets no context from the word alone
  (the same reasoning as entry 255's shared `visitAriaExtras`).
- **The saving pill** names the cat and the timestamp ("Saving Mittens · 06:14…"). That is
  entry 295's actual scope and it matters more now: the visit has left the screen by the time
  the write lands, so the pill is the only thing that can answer *whether my Mittens went
  through*.

**`⚑ Label later`** is the existing flag toggle — same endpoint, same overlap-keyed
idempotency, same un-mark — shortened from today's `Mark for labelling` to fit beside the
confirm button, and reading `⚑ Marked` once set. Deliberately no `✓` on the marked state: it
would collide with the confirm button's own glyph, so the existing `.marked` accent carries it
(entry 471's rule applies — the state goes on the element that has its own qualified CSS rule).
It stays available on *every* visit, which is what lets it stand in for everything the picker
would have done: a wrong stored label, a stranger, a wind trigger, and a visit with no
detection at all. One wording everywhere, deliberately — a control that renames itself by
context is a second state to keep consistent.

**Footer geometry is a real constraint, not a detail.** The two buttons share one row on a
~360px phone, in a footer that already carries the conditional Analyse button, the `aria-live`
readout and the visit nav — and the dialog is capped at 94vh with `overflow: hidden`, where
the failure mode is clipping the nav (entry 253). Both labels are therefore short and of
constant width, and no cat name appears on a button, so a long one (`Store Sultan`) cannot
widen the row. Verify the row at 360px with the Analyse button *also* present — that is the
widest state, and it is reachable.

**A confirmed label advances** to the next older visit, since it is terminal for that visit
and admin's annotation advances instantly — the audit is a walk and stopping on each decided
visit costs a tap. The ⚑ does **not** advance: it is a reversible toggle whose success is
deliberately silent (entry 286), so advancing would leave nothing on screen saying the mark
took.

Because it advances, **a failed write must name its own visit.** The reader has already moved
on, so a 409 (identity diverged, or the span turned out contested) or a transport failure
cannot report onto the visit-scoped readout — that is entry 228's bug, and entry 288's rule
that a job must be judged by its own record rather than a shared field. It surfaces on the
persistent line instead, naming what it was ("Mittens · 06:14 — not saved, that visit
changed"), so the one thing the operator loses is never the knowledge that they lost it.

When the span already holds labels the footer says so (`Labelled: Mittens`), with the ⚑
available as the recourse. Finding a wrong label is the audit's whole purpose, and the card's
identity comes from the *model*, not from the label — so a visit labelled Mittens and
identified Sultan is invisible today.

**A span can hold both**, and that is the normal case rather than an edge: an event's motion
cluster GROWS as later frames land (entry 224), so a cat that lingers after a confirmation
leaves the visit part-labelled with an undecided tail. `can_confirm` therefore keys on
`n_undecided > 0` **alone** — `existing` is informational, and the footer shows both
(`Labelled: Mittens · 30 more frames`). Gating on `existing` being empty instead would quietly
strand every late frame of every confirmed visit, on the visits that lingered longest and so
carry the most crops. The safety case is unaffected: the re-resolve runs over the whole span
including the new frames, so a tail in which two cats share a frame turns the span contested
and the button disappears.

**The probe is per event, not per modal.** Prev/Next repoints the open dialog at another
visit, which is exactly how entry 228's bug happened — a shared footer control reporting onto
a visit the reader had already left. So the probe refetches on every hop, its result is keyed
to the event object (the identity the rail rows and the player address a visit by, entry 284),
it carries a sequence guard against out-of-order responses (entries 235/262/366), and the
confirm button's busy state is per visit rather than a single global flag (entry 289). The
`cat_id` concurrency check is the backstop underneath all of that: even a stale button cannot
write a name the server no longer agrees with.

### `GET /api/label/visit?start_id=&end_id=`

The gate and the readout, one span-scoped read:

```
{can_confirm: bool, cat_id: int|null, cat_name: str|null,
 n_undecided: int, max_cats_in_frame: int,
 existing: [{label_kind, cat_id, cat_name, n_frames, mixed}] | [],
 reason: 'ok' | 'no_crop' | 'all_labelled' | 'unnamed' | 'retired' | 'contested'}
```

`n_undecided` counts the frames a tap would write, so the footer can say what it contributes
the way admin's stage does (entry 413) instead of asserting a bare success. `existing` comes
from `labeled_visits` scoped to the same span (`store.py:5727`), which already resolves label
kind, cat name, and the `mixed` flag.

`reason: 'no_crop'` is named for what is MISSING rather than `no_detection`: the queue's
confidence floor means a span can hold faint sub-floor verdicts and still land here, so
claiming nothing was detected would send the reader to Analyse — which re-runs the detector
and finds the same faint boxes. It asserts nothing about whether the detector *looked*,
which is the coverage-vs-verdict distinction of entries 226/279/280, and is why the Analyse
button stays reachable there. `reason: 'contested'` gets
its own footer line (`Two cats in one frame — label it later`), since "no button" without a
reason reads as a bug on the one visit type most worth a human. It names the frame, not the
cats: a box carries a class, not an identity, so "two cats were in shot" is knowable while
*which two* is not — `max_cats_in_frame` is the honest shape.

**The 0.3 confidence floor comes along, deliberately.** `_present_frames` admits only frames
carrying a cat box at or above `_ANNOTATE_MIN_CONF` (`store.py:5230`), so the phone writes
exactly the universe the desk queue works. This **diverges from entry 225**, which left
*flagged* spans unfloored on the grounds that a human pointing at one visit is not the
phantom-detection case the floor guards — and the difference is what the human then does. A
flagged span is reviewed crop by crop at the desk; a phone Yes is a blind bulk write over
whatever the span holds, where a faint 0.2 box could be an empty scene. Recorded here because
the precedent points the other way and would otherwise invite a "fix".

### `POST /api/label/visit`

Bounds validated as above; then re-resolve the span's identity and its vote spread, compare
with the submitted `cat_id`, and refuse on divergence (409) or contest (409). On agreement:
resolve the span's undecided present frames via the same `annotation_visits` path admin's
queue uses, flatten to the `LabelFrame` shape, and run them through the existing
`_validate_label` → `_commit_label` pair. That reuse is the point — the roster check, the
crop-first ordering (a crash orphans a harmless file, never a row without its crop), the
persisted `crop_geometry` read once per request (entry 467), and `add_dataset_items`'
`(src_frame_id, src_recv_ts)` UNIQUE dedup all come along unchanged, so a phone label is
byte-for-byte a desk label apart from its `source`.

Returns `{inserted, crops_written, cat_id, flag_cleared}`. `inserted: 0` is a **200**, not an
error: the frames aged out between probe and tap, the routine retention path and the reason
the flag survives (entry 227). The client reports it as "those frames have aged out", not as
a success. `flag_cleared` is what the client folds into its own flag list, as a delta rather
than a snapshot — entry 228's rule, so a mark made elsewhere meanwhile survives.

Concurrency below this layer is already handled — two labellers, or a double-tap, hit the
UNIQUE index and the first write wins — so the route adds no lock of its own.

### Interaction with the annotation queue

A labelled visit leaves the queue by construction: membership is *undecided*, i.e. no
`dataset_items` row (entry 233). So the phone drains the same queue the desk works, with no
coordination between them and nothing to tell the queue. Worth stating plainly because it is
the feature's leverage: confirmations made from the couch shrink the desk's backlog, and the
visits the phone *can't* decide are exactly the ones worth a human at a screen.

### Recoverability

A mis-tap writes a durable row in the table that survives eviction and `clear()`. It is
recoverable: admin's Labelled mode is a homogeneous per-label grid built precisely so a wrong
label leaps out (entries 241–244), `requeue` sends the visit back, and `source` makes the
phone-written subset filterable when reviewing. That existing surface is why this spec adds
no undo of its own.

## Alternatives considered

- **A cat picker in the modal** (roster avatars + stranger + not-a-cat). Rejected by the
  owner: it puts identity decisions on a phone-sized top-down crop, and the desk already has
  the roster, the crop-beside-frame player and the grade tally. Confirm/dispute keeps the
  phone to the judgment it is actually good at.
- **Client assembles the payload, reusing `POST /api/label` untouched.** Probe
  `/api/label/queue` scoped to the span, then send admin's exact body. Zero backend additions,
  but the phone would hold ~100 bboxes per visit, the second dashboard would duplicate admin's
  payload assembly (the two share no JS by design), and the span-holds-several-visits merge
  rule would live in the client.
- **The phone writes a *suggestion*** — `label_flags` gaining a `suggested_cat_id`, confirmed
  at the desk. Now moot: with no picker there is no suggestion to carry, and the ⚑ already
  routes the visit to the desk.
- **A contextual wording on the ⚑** (`No` beside a confirm button, `Mark for labelling` when
  standing alone). Rejected by the owner: one control with one wording, and the pair has to
  fit one phone row regardless.
- **Naming the cat on the confirm button** (`✓ It's Mittens`). Rejected for footer width: a
  long name (`Store Sultan`) would need ellipsis truncation on the one control whose whole
  purpose is being unambiguous. The name moved to the accessible name and the saving pill
  instead, where nothing competes for the space.
- **Adding `can_confirm` to `/api/events`.** Rejected for the lazy probe: one visit is open at
  a time, and per-span reads inside `events()` are the exact cost entries 256 and 265 had to
  undo twice.
- **Labelling only the frames whose own nearest match is the winner**, instead of refusing a
  contested span. Rejected: it silently splits a visit, leaves the other cat's frames
  undecided but unreachable from the phone, and leans on the per-frame nearest match — the
  very signal whose unreliability is why a human is being asked.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Two files (`compute/api/app.py`, `compute/api/web/user/index.html`)
  plus tests, and the halves are not separable in practice: the client's per-event probe
  keying, the auto-advance error path and the contested/409 states are all readings of the same
  contract, and the mandated browser pass needs both ends running anyway.
- **Nothing here is irreversible** — no schema change (`dataset_items.source` already exists),
  no migration, no `(breaking)` decision — so the fan-out and adversarial-verification cost of
  a workflow buys nothing over one careful pass.
- The judgment sits in three places worth the strong tier: the vote-spread guard, the
  probe-vs-write concurrency semantics, and the write-drain UI. Everything else is reuse.

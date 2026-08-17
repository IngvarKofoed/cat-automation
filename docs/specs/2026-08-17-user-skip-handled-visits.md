# Skip already-handled visits when stepping through the feed

The user app's playback modal is now where the household works through visits — confirm with
**Yes**, defer with **⚑ Label later**. A pass down the feed therefore keeps re-landing on
visits already dealt with, and there is no way to tell the nav to move past them. So the
modal gets one checkbox, **Skip done**: with it ticked, `Older` / `Newer` (and the swipe,
which shares the same code path) land only on visits that carry neither a label nor a ⚑.

It is a *navigation* filter, not a list filter. The feed keeps showing every visit, so a
handled one is still one tap away in the rail — only the hop skips it. That is what keeps the
position readout (`12 / 40`) meaning what it means today; the index simply jumps by more than
one. And because the state now travels with the feed, the rail marks it: a labelled visit gets
a **✓** beside the ⚑ it already gets when marked, so what the nav jumps over is visible
without stepping into it.

The ⚑ half needs nothing new: `isFlagged(ev)` (`user/index.html:1388`) is already a pure
client-side span-overlap test over the flag list fetched with each page-1 load, which covers
appended pages too — `GET /api/label/flags` is unscoped by span. It is not unbounded, though:
`list_label_flags` returns the newest `_MAX_FLAGS_READ` (500) flags, so past 500 outstanding
flags the oldest are invisible to `isFlagged` and the skip re-lands on them. That fails in the
direction this design calls honest everywhere else — a handled visit reappears, never a fresh
one silently skipped — and it is pre-existing (the rail's ⚑ is already missing there), so it is
recorded rather than fixed here. The label
half does: today "has this visit been labelled" is only knowable per visit, after opening it,
via `GET /api/label/visit` → `Store.visit_label_state` (`store.py:5727`). So the feed starts
carrying it.

## Key decisions

- **The feed carries a `labelled` flag** (extends). `Store.events` gains `with_labels`, the
  `with_subject` precedent (entry 257): when set, each event gets `labelled` — whether its span
  holds a `dataset_items` row. A boolean, not a count, because nothing reads the magnitude: the
  predicate, the ✓ and the local mirror all reduce to yes-or-no, and `EXISTS` stops at the
  first live row per span where `COUNT` walks them all plus a `frames` probe each, ×100 spans
  per page under the shared write lock (entries 102–105/354/401 budget exactly that).
  `/api/events` opts in; `cats_overview` (`store.py:7855`,
  already `with_subject=False`) and `door_stats` (`store.py:8001`, which pages up to 8×500
  events for counts alone) do not, so neither pays for a field nothing there reads.
- **One indexed read per event span, joined on the clear-safe pair** (reuses). Per-span, not
  one envelope read over the page — the shape `events()` already argues for at length
  (entries 256/265), and here the range seek rides `idx_dataset_src`'s leading column. The
  read `JOIN`s `frames` on **both** `src_frame_id` *and* `src_recv_ts`, the pair every
  sibling reader keys on: `frames.id` has no AUTOINCREMENT and `clear()` deliberately spares
  `dataset_items`, so without it a stale pre-clear row makes a brand-new visit read as
  handled and the nav skips a visit nobody has ever looked at (entry 490, whose symptom was
  the same table lying about the same thing).
- **"Handled" means any label row, or an overlapping ⚑** (new). Not `all_labelled`: an
  event's cluster *grows* as later motion lands (entries 224/482), so a visit that was
  confirmed routinely regains an undecided tail and would come back — the opposite of what
  the checkbox is for. Any row also means a desk decision (*stranger*, *not a cat*) counts as
  handled, which is right: those are "I've dealt with this" too.
- **Skipping applies in both directions, and to the swipe** (extends). The swipe calls the
  same `moveEvent`, and its rubber-band damping reads `canMove(delta)` *during* `touchmove` —
  which is the reason the state has to travel with the feed rather than be probed per hop:
  an async answer cannot drive a gesture that is already under the reader's thumb. A
  one-directional skip would also strand the reader: skip forward past three visits, tap
  `Newer`, and you are back on the first one you skipped.
- **One search function, three consumers** (extends). `nextIndex(delta)` returns the index
  the hop would land on, or `-1`. `canMove` becomes `nextIndex(delta) >= 0`; `moveEvent` and
  `updatePlayerNav`'s `disabled` both read it. Today those three each re-derive the bound
  inline, which is survivable only while the bound is `± 1`.
- **`Older` is enabled whenever a page could still be fetched** (diverges). Today
  `updatePlayerNav` disables it purely on index bounds (`user/index.html:2518`), so at the
  last loaded visit only a *swipe* reaches the fetch-next-page branch (entry 264) — a reader
  without a touchscreen is dead-ended at every page edge. With the skip on, a whole page of
  handled visits leaves nothing reachable, so the button has to offer what the swipe already
  does. **Unconditionally, not only while the box is ticked** — one rule, and the dead-end is
  worth fixing for everyone. So this is a deliberate change to behaviour with the feature
  turned *off*, and the only one: a desktop reader who never ticks the box finds `Older` live
  at a page edge where it used to grey out. A press there **completes the hop** once the page
  lands, unlike a swipe, which keeps refusing — see *What a refused hop says*.
- **The rail marks a labelled visit with ✓** (extends). A sibling to `flagMark()` in the same
  `.chip-row`, `aria-hidden` for the same reason, with the sentence added to
  `visitAriaExtras` — the one shared source the row *and* the dialog `aria-label` both read,
  so the two can never claim different things about one visit (entry 255). The incremental
  repaint path generalises with it: `renderActivityRowFlag` (`user/index.html:1831`) becomes
  `renderActivityRowMarks`, owning both marks and `setRowLabel`, rather than growing a second
  near-identical function — a confirm can clear a ⚑ *and* add a label, which is one repaint.
- **The checkbox never touches the visible set** (diverges). `strangersOnly` and `showAll`
  both `closePlayer()` and re-render, because they change `activityVisible` and every index
  with it (`user/index.html:2208`). This one changes no membership: it re-renders nothing,
  closes nothing, and only repaints the nav. Diverging from the sibling toggles' handler is
  the point — closing the modal on a control that lives *inside* the modal would be absurd.
- **Off by default, in-memory** (reuses). Plain module state reset on reload, like the two
  toggles above; the user app uses no `localStorage` today and one checkbox does not earn the
  first. Off because a reader who never asked must not have visits silently withheld.

## Goals

- Walking older (or newer) through the feed lands only on visits carrying neither a label nor
  a ⚑, while the checkbox is ticked. Note what that is *not*: a visit nobody can decide from a
  phone — no name to say yes to, `unanalyzed`, no crop — carries neither mark, so it keeps
  coming back on every pass (see the second Non-goal). The phone's only way to retire one is a
  ⚑, which means something else and lands it on the desk's list; making those skippable needs
  confirmability in the payload, and is deliberately not this change.
- The skip covers both halves of "handled" — a **Yes** label and a **⚑** — and keeps working
  for writes made in this session without a feed refetch.
- Which visits count as handled is legible from the rail, not only inferable from the nav
  jumping.
- Nothing is silently withheld: the checkbox is off by default, the rail still shows every
  visit, and a refused hop says *why* in words that stay true.

## Non-goals

- Hiding handled visits from the feed itself. The rail is how a handled visit stays
  reachable, and the existing toggles already own list membership.
- Skipping visits with nothing to label at all (`no_crop`, or no name to say yes to). That
  state is not in the feed payload — it needs `visit_label_state`'s `_present_frames` read per
  span — and those are exactly the visits a reader may want to ⚑ or Analyse rather than pass.
- Any change to what **Yes** or **⚑** write, or to `visit_label_state`. This change reads.
- Skipping on the admin dashboard's Activity page. Different file, no shared JS (M4), and the
  desk works the annotation queue rather than the feed.

## Design

### Backend: `labelled` per event

In `Store.events`, beside the existing per-span subject / corruption / identity reads and
under the same lock discipline:

```sql
SELECT 1 FROM dataset_items d
  JOIN frames f ON f.id = d.src_frame_id AND f.recv_ts = d.src_recv_ts
 WHERE d.src_frame_id BETWEEN ? AND ? LIMIT 1
```

one execute per span, gated on `with_labels`. The `JOIN` is what makes the answer clear-safe;
it also drops a label whose source frame has since evicted, which is the deliberate trade —
`visit_label_state` keeps such rows (a label outliving its frame is the table's purpose), but
for *this* question a frame that is gone cannot be re-labelled anyway, and the honest failure
direction is a visit reappearing rather than one being silently skipped.

`/api/events` passes `with_labels=True`. The field is additive: absent it, every existing
consumer of the payload is byte-identical.

No new index. `idx_dataset_src` is `UNIQUE(src_frame_id, src_recv_ts)`, so the range seek is
index-served and `dataset_items` is sparse — one row per *labelled* frame, not per frame — so
a span's read stops at its first live row where the subject read walks every verdict in it.

**The plan is pinned by a test, not asserted in prose.** Measured on the real schema with no
`ANALYZE` stats — a benched plan *with* stats is a plan the real store never gets (entry 266):

```
SEARCH d USING COVERING INDEX idx_dataset_src (src_frame_id>? AND src_frame_id<?)
SEARCH f USING COVERING INDEX idx_frames_recv_ts (recv_ts=? AND rowid=?)
```

Both index-only, and the cost is the span's *labelled* rows — already the right shape, so
unlike the scoped scorecard (entry 391) this needs no `CROSS JOIN` pin (measured: identical
plan). The regression to guard is `frames` becoming the outer loop, which walks every *frame*
in the span probing `dataset_items` per row — the visit's whole length rather than its handful
of labels, per span, under the write lock. Note it is **not** a full table scan: SQLite does
transfer the range through the `f.id = d.src_frame_id` equality, so forcing that order still
yields a seek. Either way the returned flag is identical, which is how this class has recurred
five times (entries 229/265/276/307/385/391), so an `EXPLAIN QUERY PLAN` assertion goes in
beside the scorecard's, per entry 396.

### Client: the predicate, and the search

```js
function isHandled(ev) { return !!ev.labelled || isFlagged(ev); }
```

`nextIndex(delta)` walks `activityVisible` from `activitySelectedIndex + delta` in `delta`'s
direction and returns the first index where `!skipDone || !isHandled(ev)`, or `-1` if the
walk runs off the end. Everything else follows from that:

- `canMove(delta)` → `nextIndex(delta) >= 0` — so the swipe's damping and the buttons'
  `disabled` state agree with what a hop will actually do.
- `moveEvent(delta)` → `openEvent(nextIndex(delta))` when it is `>= 0`; otherwise today's
  refusal branches, chosen by direction.

### What a refused hop says

The existing wording rule holds: a note must not assert an absolute it cannot support, and
`nav` walks the *filtered* list, so a hiding toggle already forces the "shown" hedge
(entry 254). With the skip on, the refusals become — reusing the same
`filtered ? … : …` construction —

| Direction | skip off (today) | skip on |
|---|---|---|
| newer, nothing left | `newest` / `newest shown` | `nothing newer to do` / `nothing newer shown to do` |
| older, nothing left, not truncated | `oldest` / `oldest shown` | `nothing older to do` / `nothing older shown to do` |
| older, nothing left, truncated, fetch in flight | `loading older…` | `loading older…` |
| older, nothing left, truncated, **idle** | *(unreachable)* | `nothing loaded to do` / `nothing shown to do` |

`loading older…` is claimed only once a fetch is genuinely in flight: `loadOlderActivity` bails
synchronously at the store's first frame (clearing `truncated` as it goes), and that case falls
through to the end-of-feed wording rather than promising a page nothing will deliver.

The paging branch keeps its shape — refuse *this* hop, kick `loadOlderActivity()`, note
`loading older…` — with two changes at the landing. `loadOlderActivity` today blanks that note
on the grounds that "the hop it refused now works" (entry 264), which with the skip on may be
false: the page can be wholly handled visits. So the landing **recomputes** the note from
`nextIndex(1)` instead of clearing it.

And a **click** completes the hop the landing made possible; a **swipe** does not. Entry 264's
reason for refusing outright is that holding the frame still mid-gesture reads as a freeze —
which is a swipe argument, and void for a button: there is no frame under a thumb, and a
press that visibly does nothing but change a caption, then works on the second press, is a
broken-feeling control. So the button handler records a pending hop, and the landing consumes
it: hop if `nextIndex(1)` now answers, otherwise leave the note (and *no* chained fetch — one
page per press stands, so the next press pulls the next page). The intent is dropped, never
queued, if anything else moved meanwhile — the modal closed, the reader navigated, or a page-1
reload superseded the fetch, which is the same `seq` guard the append itself already takes.
The swipe sets no intent, so its ease-back stays exactly as it is.

That recompute is what makes the fourth row above reachable, and it is the feature's *steady
state*, not a corner: once the household keeps up with confirmations, everything past the
recent few is handled, so the first deep `Older` tap lands there. Both of the obvious notes
would be false — `loading older…` when nothing is loading, `nothing older to do` when
untouched pages exist — so it gets its own wording, scoped to what is loaded (and hedged to
`shown` when a toggle is hiding events, per the rule above). It does **not** promise more
exists: `Older` staying enabled carries that half, which is exactly what the enabled-when-a-
page-could-be-fetched decision buys.

One tap pulls at most **one** page. A bounded 2–3 page chain would also be defensible — an
append disturbs nothing on screen (entry 261), so a chained *fetch* moves no frame — but one
page keeps the tap's cost predictable, and the reader who wants the whole backlog can untick
the box. The `older visits failed` branch is untouched.

### The rail's ✓

`labelMark()` mirrors `flagMark()` — a `✓` span, `aria-hidden`, `title` "Labelled — newer
frames may have arrived since" — appended to the row's `.chip-row` ahead of the ⚑ (the decision came first; both together is
rare, since a phone confirm clears an overlapping flag, but a desk label plus a live flag can
produce it). `visitAriaExtras` gains the matching sentence, which is what actually carries the
state to a screen reader for *both* the row and the dialog.

The title hedges rather than saying "already labelled" flat, because the mark and the modal
can legitimately disagree: a part-labelled span still offers **Yes** for its undecided tail
(entry 482), so a ✓ claiming the visit is finished would be contradicted the moment the reader
opens it. What the mark asserts is that a decision exists here — which is exactly what the
skip acts on.

`renderActivityRowFlag` becomes `renderActivityRowMarks(ev)`: same lookup-by-identity, same
"make a `.chip-row` if the card has none" fallback — with its early return widened from "not
flagged" to "neither mark wanted" — and it now adds/removes both marks and re-runs
`setRowLabel`. Its callers are the ⚑ toggle and the confirm path, both of which can move
either mark.

### Keeping local writes in step

Both writes already mirror themselves locally rather than refetching the feed, and the skip
predicate has to see that:

- **⚑** — free. `toggleFlag` maintains `activityFlags`, and `isFlagged` reads it.
- **Yes** — on a response with `inserted > 0`, set the event's own `labelled` (the same
  object identity the rail rows, the player and the paging state address a visit by, per
  entry 284) and repaint that row's marks, so the ✓ appears without a refetch — today the
  confirm path repaints only when the server reports `flag_cleared`. A `200` with
  `inserted: 0` — the frames aged out — must *not* mark it handled; that path already reports
  its own failure.

`eventRow` draws both marks at build time from the same predicates, so a page-1 reload or an
appended page paints ✓ on visits labelled in an earlier session, not only on this session's
writes.

Either mirror also repaints the **nav**, not just the row. What a mark changes is the
*neighbours'* reachability — the search starts at `index ± 1`, so marking the visit on screen
changes its own buttons not at all, while marking the one just left can empty the direction it
sits in. Without that repaint `Newer` sits enabled over a visit the skip now steps past:
live-looking and inert, which is the failure the enabled-at-a-page-edge decision exists to
avoid. Any `nothing … to do` note is dropped at the same moment, having described a search over
the handled state that just changed. *(Found in the browser, not in the design.)*

Note the order: `confirmVisit` advances *first* and the write settles behind the reader, so
the hop out of a visit is chosen before its own label exists. That is fine — the target is
some other visit — but it does mean an immediate `Newer` lands back on the visit just
confirmed, until the write returns. Which is honest: the label is not written yet.

### When the backend is older than the page

`frontend-dev.sh` serves the working tree's HTML against the real compute PC, so a page that
expects `labelled` can meet an `/api/events` that has never heard of it — and a skip that
silently degrades to ⚑-only would *under*-skip while looking like it works. So the checkbox is
**disabled** with a stated reason ("can't check labels — the compute PC may be on an older
build"), rather than half working. Entries 164/173/183/365/489's rule: a readout must name the
deployment gap, not only the benign cause.

The detection is **key presence** (`'labelled' in ev`), never truthiness, and it is latched
once per page-1 payload:

- **Presence, because absent and `false` are different claims.** A current backend answers
  `labelled: false` on every event of a feed nobody has labelled yet — which is the *rollout*
  state — so a falsiness test would banner a healthy system as an old build. The trap is live:
  `isHandled` reads the same field for its own yes-or-no, so the two predicates must not be
  confused with each other.
- **Latched at load, because the confirm path mutates the field.** The local mirror would
  *create* `labelled` on an event fetched from an old backend (`POST /api/label/visit`
  predates this feature and already answers `inserted`), re-enabling the checkbox mid-session
  into exactly the half-working state — ⚑ plus this session's own confirms — that disabling it
  exists to prevent. So the mirror is a no-op when the load carried no field.

The reason renders **visibly**, in the footer's existing `.msg` line, not only in the
checkbox's `title`: a phone has no hover, so a greyed control with no words beside it reads as
broken, which is the state entry 489 exists to prevent.

### Where the checkbox goes

In the modal's `.player-actions` footer, on the `.visit-nav` line — it acts on that pair, so
it belongs beside them. That row is already tight (entries 253/488: the dialog column's
height budget, and `flex` on the footer's own children), so the label is short — **Skip
done** — with the sentence in its `title`: "Older / Newer jump past visits already labelled
or marked ⚑". Below the narrow breakpoint the nav already takes its own full-width line
(entry 250); the checkbox rides above it.

It **blurs itself on change**, joining the scrub slider that already does (entry 298). The
modal's keydown handler drops any keystroke targeting an `INPUT` (`user/index.html:2620`), so
focus left on a checkbox swallows Space (play/pause) and the arrows (scrub) silently — the
failure entries 235/242/298 each fixed on the admin page, arriving here.

## Alternatives considered

- **A companion `GET /api/label/labelled` endpoint**, fetched beside each page like
  `/api/label/flags`, giving one consistent overlap-test mechanism for both halves of
  "handled". Rejected: it leaves `events()` untouched but costs a second request per page and
  needs the per-frame label rows coalesced into ranges to keep the payload small — machinery
  the per-event count does not need.
- **Probe `/api/label/visit` per candidate hop.** No backend change at all, but it is a
  serial round trip per skipped visit, each heavier than the count (it does `_present_frames`
  plus the identity join), and `canMove` could no longer answer the swipe synchronously — the
  rubber-band would have to guess whether the hop it is damping can happen.
- **Skip on `all_labelled` rather than any row.** More literally "already said yes", but it
  needs a `_present_frames` read per span, and a grown span brings a confirmed visit back.
- **Filter handled visits out of `activityVisible`.** One-line change, reusing the toggle
  path — and wrong: it would renumber the feed, close the player on every tick of the box,
  and leave a handled visit unreachable when the reader wants to check what they wrote.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** One field threads through three files — `Store.events`, the
  `/api/events` route, and the SPA — and the client work (`nextIndex` replacing three inline
  bounds, the note states, the ✓ marks) is one interlocking code path where parallel agents
  would only conflict.
- The judgement is in the client: the wording states and the `nextIndex` refactor have to be
  read against what `moveEvent` / `updatePlayerNav` / the swipe already promise, which is
  interpretation, not transcription.
- Verification per this repo's rules: pytest, the `EXPLAIN QUERY PLAN` pin above, and a
  Playwright pass over the modal — confirming the page under test is the page on disk first
  (entry 454), and reading the checkbox's disabled state by **computed style**, never
  `classList` (entry 287).

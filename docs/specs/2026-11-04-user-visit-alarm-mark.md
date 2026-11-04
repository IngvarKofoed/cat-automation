# Alarm mark on the visit that set it off

The user app's Activity feed shows no sign of the foreign-cat alarm. You can only tell
that a neighbour was chased off by opening the admin Start card and matching times by hand.
This adds a small speaker mark to the card of the visit that **set the alarm off**,
plus one line in the player saying how long it sounded and who it took the cat for.

The server attaches each alarm episode from the `actions` log to the one visit whose time
span holds its deciding frame. It does this as an opt-in annotation on `/api/events`, the
same way `labelled` rides the feed today. No schema change.

This lifts the foreign-cat-alarm spec's non-goal "The user app. Alarms are not shown there
yet" (`docs/specs/2026-11-01-foreign-cat-alarm.md`).

## Outcome

**What you get:**

- A speaker mark on the Activity card of each visit that set the alarm off. It is plain
  when the alarm played and crossed out when it triggered but nothing played.
- An **Alarm** line in that visit's player, giving how long it sounded, how many clips,
  and what the alarm took the cat for.
- A visit that set the alarm off stays in the feed whatever the filters say ("Hide our
  cats", or "Show all" off).

**How to verify:**

- Let a foreign cat set the alarm off with the switch On. Its visit's card shows the mark,
  and no other visit's does. The player line reads "Sounded <duration> · M clips, judged …",
  and the duration and M match that alarm's row in the admin Start card's Recent list.
- Watch the feed (player closed) while an alarm runs. The mark appears while the cat is
  there. After the alarm stops, the player says "Sounded…", not "Sounding now", without a
  manual reload.
- Turn the Pi's speaker switch off and trigger again. That visit shows the crossed-out
  speaker, and its line says the Pi's speaker was off.
- Tick "Hide our cats" over a feed where an alarm visit is named as a resident. That visit
  stays listed and every other resident visit is hidden.

## Key decisions

- **Attached to the feed, server-side** (extends). `Store.events` gains an opt-in
  `with_alarms`, mirroring `with_labels` (changelog 503). Only `/api/events` passes it, so
  `cats_overview` and `door_stats` never read `actions`. Each event then carries `alarms`:
  a list, empty when none.
- **Attributed by the deciding frame's time, to exactly one visit** (new). An episode
  belongs to the event with `start_ts ≤ frame_recv_ts ≤ end_ts + _VISIT_GAP_MS`.
  - These windows never overlap: clusters are split by gaps longer than `_VISIT_GAP_MS`.
    So each alarm lands on at most one visit.
  - The `+ _VISIT_GAP_MS` covers Tuning, where the deciding frame can be a non-motion frame
    just after the visit's last motion frame.
  - Matching is by time, not by id, because `frames.id` is reused after a Clear
    (changelog 490/504) and `recv_ts` is not.
- **Only the visit that set it off** (by decision). Two cases therefore get no mark: a
  visit that merely overlaps the noise (a resident arriving in the ~3 s tail), and a later
  visit while a long alarm is still sounding. The admin Start card's log stays the full
  record.
- **Two marks on the card, three states in words** (new). The card shows a speaker
  (sounding or sounded) or a crossed-out speaker (silenced: it triggered but nothing
  played, because the Pi's switch was off or playback failed). Silenced gets its own glyph,
  so "judged foreign but heard nothing" never passes for a real chase-off. Sounding vs
  sounded is told apart only in the player line and the spoken label: a phone has no hover
  for a `title` (changelog 512), and "sounding" goes stale too easily to earn its own glyph.
- **The player line carries the alarm's own verdict** (extends). It is one row in the
  existing stats grid. The alarm decides on ~10 frames in 5 s while the card's name comes
  from the whole visit, so the two can disagree. "Judged an unknown cat" under a card
  saying *Mittens* is exactly the case worth seeing.
- **The feed nudges when an alarm starts or stops** (extends). `activity_signal` gains
  `alarm_rev`. Changelog 285 refused a verdict counter because the oracle writes
  continuously. This one moves twice per alarm, so it costs nothing.
- **The feed's filters never hide an alarm visit** (extends). Neither "Hide our cats" nor
  the default noise filter hides one. The alarm judged that visit foreign, so hiding it
  would hide exactly the case worth checking, e.g. a resident the alarm went off at. This
  is the same move as exempting `unanalyzed` from the noise filter (changelog 283).
- **One unindexed read per page** (reuses). `actions` holds one row per episode, tens a
  day at most. A single envelope read per page is right here. Changelog 256's per-span
  rule is about the high-cardinality frame/verdict tables.

## Goals

- At a glance in the feed, see which visits set the alarm off, and whether anything
  actually played.
- In the player, see how long it sounded, how many clips, and what the alarm took the cat for.
- A screen reader hears the same facts.

## Non-goals

- Marking every visit the alarm sounded *during* (rejected above).
- The admin Activity feed. It receives the key and ignores it; the Start card is admin's
  alarm view.
- Alarm counts on the Visits page.
- Test plays (`mode = 'test'`), and the Pi's own Play buttons, which write no row.
- A live clip count while sounding; a link to the deciding frame; the full `ended`
  vocabulary. The admin log has these.

## Design

### Data: `Store.events(..., with_alarms=True)`

This runs after the `limit` cap, so its cost is bounded by the page.

- **Envelope.** `lo` = the page's smallest `start_ts`; `hi` = its largest
  `end_ts + _VISIT_GAP_MS`.
- **One read.** It selects `start_ts, end_ts, ended, verdict, cat_id, clips_played,
  clips_failed, pi_disabled, frame_recv_ts`, plus `cats.name` via a LEFT JOIN as
  `list_actions` does, `WHERE mode = 'on' AND frame_recv_ts BETWEEN lo AND hi`.
- **Assignment.** Python gives each row to the event whose window holds its
  `frame_recv_ts`, and discards the rest.

Each event gets `alarms`: those rows in start order, as `{start_ts, end_ts, ended,
verdict, cat_id, cat_name, clips_played, clips_failed, pi_disabled}` with `pi_disabled` a
bool. The key is always present under `with_alarms`, even as an empty list. Several
alarms can share one visit, e.g. an alarm switched off and then on again mid-visit.

**Why the deciding frame almost always falls in that window.** The deciding frame is the
newest counted frame received in the last 5 s (`deterrent.py` `maybe_start`).

- **Collecting and Running.** Capture is motion-only, so every counted frame is a stored
  motion frame inside a cluster.
- **Tuning.** The alarm step identifies the open visit's whole id span, non-motion frames
  included. A visit is open only while its newest motion frame is within `_VISIT_GAP_MS` of
  now, so the deciding frame lies in the cluster or its ≤ gap tail.

**Unattributed alarms.** Its alarm then shows nowhere in the user app. Accepted: the
admin log still holds it. This happens when:

- the triggering visit is not in the feed: it was dropped by `min_frames`, was the scan
  cap's partial oldest cluster, or has been evicted;
- in Tuning only, the deciding frame lies outside every window. `maybe_start` also runs
  with no visit open, and `alarm_frames` counts frames identified by any path, including a
  whole-store "Identify all" that covers non-motion frames. A tail frame can also land a
  few ms past `end_ts + gap`, because the open span's newest frame is read after `now` is
  taken.

These cases are rare, and the rule fails toward no mark, never toward a mark on the wrong
visit.

### Mark state (client)

An episode is **silent** when `clips_played == 0` and either the episode has closed or it
is open with `pi_disabled` or `clips_failed > 0`. An open episode retries every tick, so an
unreachable Pi or a switched-off speaker must not read as "sounding". `pi_disabled` is
sticky once set.

`alarmState(ev)` returns `null` when `alarms` is absent or empty. Otherwise, first match wins:

1. any open, non-silent episode → **sounding**
2. any episode with `clips_played > 0` → **sounded**
3. else → **silenced**

**Older backend.** An absent `alarms` key reads the same as an empty list. Accepted, with no
older-build notice, unlike "Skip done" (changelog 512). There, a missing key left a control
that silently malfunctioned. Here it only leaves marks absent, and the backend and the page
ship together on the compute PC.

### Card

The chip row order becomes: pill, health dot, **alarm mark**, ✓, ⚑. The alarm is a fact of
the visit; ✓ and ⚑ are household actions.

- **Glyph.** The mark is an inline-SVG speaker sized like the ✓/⚑ glyphs: with sound waves
  for sounding and sounded, which share one glyph, and with a slash for silenced.
- **Not the emoji.** It is deliberately not the 🔊 emoji, which iOS draws as a colour emoji
  beside the text glyphs.
- **Colour.** Sounding and sounded use the theme's existing neighbour/foreign token, which
  reads in both themes. Silenced uses the muted ink.
- **Accessibility.** The mark is `aria-hidden`, and its `title` gives the state in words.

The mark is drawn when the card is built and refreshes with the feed reload. No per-row
repaint path is needed: no tap in the user app changes alarm state.

The existing repaint paths must keep the order. `refreshActivityRow` inserts a
re-analysed visit's health dot before `.label-mark, .flag-mark`, so `.alarm-mark` joins
that selector. Otherwise the dot lands after the alarm mark on a card with no ✓/⚑.

### Player line

`renderPlayerStats` gains an **Alarm** row when `alarms` is non-empty.

- **Signature.** It takes the event rather than `detection` alone, at both of its call
  sites.
- **Visibility.** The panel shows when either `detection` or `alarms` exists.
- **No alarm.** The row is absent, not "none". An alarm switched Off also produces no row,
  and "none" would claim more than the data supports.

There is one phrase per episode, joined with "; ". Each is chosen by the first rule that
matches, and each ends with ", judged *X*":

1. **Open and silent** → "Triggered, nothing playing" plus the reason below.
2. **Open** → "Sounding now".
3. **`clips_played == 0`** → "Triggered, nothing played" plus the reason. The reason is
   "(the Pi's speaker was off)" if `pi_disabled`, "(playback failed)" if
   `clips_failed > 0`, otherwise nothing.
4. **`ended = 'interrupted'`** → "3 clips, cut short by a restart". No duration: after a
   hard kill its `end_ts` is the relaunch time, not when the sound stopped.
5. **Otherwise** → "Sounded 1m 5s · 3 clips". If `pi_disabled`, append " · then the Pi's
   speaker was switched off". Admin's log flags the same case, and without it the duration
   overstates how long anything played.

**Duration** is `end_ts − start_ts`, formatted exactly as admin's `fmtDuration`:
`45s`, `1m 5s`, `2h 3m`. The helper is duplicated, since the two pages share no JS. The
same format is what lets the Outcome check compare against the admin log.

*X* is the cat's name for `neighbour` (falling back to "cat #id" when the cat row is gone)
and "an unknown cat" for `unknown`.

### The feed's filters

A visit with a non-empty `alarms` list passes both feed filters: "Hide our cats" even when
its card names a resident, and the default noise filter (`showAll` off) even when its
subject reads `unrecognized`/`motion_only`. The noise case is reachable in Tuning, where
the counted frames can all be non-motion tail frames outside the motion span.

The exemption is one rule in `passesActivityFilters`. Both empty-state readouts that
currently test `isResidentEvent` and `NOISE_KINDS` directly (`showAllWouldReveal`,
`hideCatsWouldReveal`) apply it too. Otherwise the empty state could tell the reader that
unticking reveals visits it never hid.

`isResidentEvent` itself is unchanged, since other callers mean "named a resident" and
nothing more. The exemption is user-app only; admin keeps its own feed rules.

### Screen reader

`visitAriaExtras` gains " Alarm sounding.", " Alarm sounded." or " Alarm triggered but
silent.". The row and the dialog share it (changelog 255).

### Refresh

`activity_signal` adds `alarm_rev`, computed as
`SELECT COUNT(*) + COUNT(end_ts) FROM actions WHERE mode = 'on'`.

- **Movement.** It moves once when an episode opens and once when it closes. Rows are never
  deleted: the log survives Clear and eviction.
- **SSE key.** The SSE endpoint builds its change key as an explicit tuple
  (`app.py` `api_events_stream`), so `alarm_rev` must be added to that tuple. Returning it
  in the dict alone nudges nothing.
- **Why it's needed.** Without the nudge, a feed whose cat has left keeps reading
  "Sounding now".
- **Known limit.** The nudged reload does not always run, so a "Sounding now" can outlive
  its alarm until the next reload. An open player keeps its line until reopened, because
  the full reload stays guarded off while playback is open (changelog 261). And
  `gentleRefresh` skips the reload while the feed is scrolled more than 200 px down.

### Docs

- **ARCHITECTURE:** the *Dashboard + API* row (the user dashboard marks the visit that set
  an alarm off), and *The foreign-cat alarm › Record*.
- **CONCEPT:** §4 *Deterrence* gains one clause saying the visit that set the alarm off is
  marked in the at-a-glance dashboard. §6 already promises "any deterrents".

## Alternatives considered

- **Mark every visit the alarm overlapped.** This would show a resident caught in the tail
  or a long alarm spanning two visits. Not chosen: the user wants one mark per alarm, on the
  visit that caused it.
- **The client joins the admin log (`/api/deterrent/log`) itself.** Rejected: that log is
  capped at the newest 20–100 rows, so older feed pages would silently lose their marks,
  and the matching rule would live in JavaScript.
- **Link by the deciding frame's id, or by `visit_start_ts` equality.** Rejected:
  - the id misses Tuning's non-motion tail and is reused after a Clear;
  - `visit_start_ts` is floored at the worker's watermark after a restart mid-visit, so it
    need not equal the event's `start_ts`.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** There is one thread across three files: `Store.events` and
  `activity_signal` in `store.py`, the route and SSE key in `app.py`, and the mark, player
  line and filter rule in `user/index.html`. The client's states and wording depend on the
  exact payload the store returns, so splitting it would only add a handoff seam.

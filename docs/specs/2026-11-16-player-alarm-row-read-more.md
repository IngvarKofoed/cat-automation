# Player Alarm row: one fixed line, the full account behind "Read more"

The user app's visit player puts an **Alarm** row first in its stats panel. Today the row is
missing for a visit with no alarm, and for an alarm visit it holds every episode's full sentence,
one per line. That makes the panel a different height from visit to visit. The dialog is
vertically centred (`.modal { align-items: center }`), so while it is under its 94vh cap any
change in the panel's height moves the picture, the Play bar and the filmstrip on every hop. At
the cap, the panel gives up height first (entry 610).

This change makes the row **always present and always exactly one line tall**. It is blank when
the visit set off no alarm. When it did, the row spans the panel's full width and shows a short
status and verdict ("Sounded · unknown cat"), with a **Read more** button that opens the full
text in a popup inside the player.

## Outcome

**What you get:**

- Hopping between visits no longer moves the stage, Play bar or filmstrip because of the Alarm
  row, whether or not a visit set off the alarm.
- An Alarm row that is always the first stats line. It is blank with no alarm, and otherwise one
  full-width line ("Sounded · Store Jihn") with Read more.
- The full alarm account, including how it decided, in a popup that closes back to the
  player, never out of it.

**How to verify:**

- Use a viewport where the dialog sits under its 94vh cap (e.g. 390×844), and confirm the check
  fails on today's build first. Then hop between an alarm visit and a no-alarm visit, and between
  a short alarm and an explained one, picking visits whose head chip and footer look alike. The
  stats panel's height, and the stage's top edge and height (`getBoundingClientRect`), are
  identical on every one.
- At 390 px and at 360 px, an alarm visit judged "unknown cat" shows its whole status on one
  line. A cat name long enough to overflow ends in "…", and Read more stays whole at the right
  edge.
- Tap Read more. The popup lists each episode's sentence (word for word what the row showed
  before this change) and the explanation. Esc, a tap outside the card, and a tap on the
  player's backdrop each close only the popup, and focus lands on Read more.
- With the popup open, Space and the arrow keys don't drive the player, Tab and Shift+Tab stay
  inside the popup, and nothing under the scrim (swipe, ×, Play, the visit arrows) can be
  reached. Closing the player, or a hop landing, removes the popup.

## Key decisions

- **The row is always there, blank without an alarm** (diverges). This reverses the 2026-11-04
  `user-visit-alarm-mark` rule that the row is "absent, not none". The blank keeps that rule's
  reason: it claims nothing, whereas "none" would be wrong for an alarm that was switched Off,
  and "—" already means *not measured* in this panel.
- **One full-width line, never wrapping, same height in both states** (new). The Alarm row
  spans both grid columns, so its status gets roughly 210 px on a 390 px phone instead of the
  roughly 139 px left beside "Read more" in the value column. The status text is cut with "…"
  when the line is still too narrow; "Read more" is never cut. An alarm row and a blank row are
  the same height, so the panel's height no longer depends on the visit.
- **The short line is "*state* · *verdict*"** (extends). It reuses `alarmState(ev)` for the
  state and `alarmPhrase`'s way of naming the cat, made terser so the common case fits. The
  verdict stays visible at a glance, which is why entry 611 put this row first.
- **The full text moves, unchanged, into the popup** (extends). The popup holds one paragraph
  per episode, built by today's `alarmText(a, ev)`, followed by the row's current `title`
  explanation, which a phone could never show since it has no hover. `alarmPhrase`,
  `alarmDecided` and `alarmText` are not changed.
- **The popup is an overlay inside the player dialog** (reuses). It is the same div-overlay
  pattern as `.modal`/`.dialog`, scoped to `#playerDialog`. While it is open, the rest of the
  dialog is `inert` and Tab is held on its Close button. Not a native `<dialog>` and not the Popover API: the page has one
  modal mechanism, and either native one would still need the same Esc guard against the
  player's own key handler.
- **While the popup is open, every dismissal closes the popup only** (extends). Esc, a tap on
  the dimmed player, and a tap on the player's own backdrop each close the popup and leave the
  player open. Opening it drops a hop that was waiting on a page fetch, by the same "the reader
  has moved" rule `openEvent` already applies.
- **Its own qualified hidden rule** (reuses). The page has no bare `.hidden` rule (changelog
  287), so the popup needs one of its own.

## Goals

- The stage, Play bar and filmstrip keep their position when hopping between an alarm visit and
  one without, and between alarm visits whose text differs in length.
- An alarm's state and verdict can still be read at a glance in the player.
- The full account, including how the alarm decided, stays one tap away.

## Non-goals

- Changing any alarm wording in the popup, the card's speaker mark, the feed filters, or the
  screen-reader text in `visitAriaExtras`.
- Everything else that changes the dialog's height. A visit with no identity chip
  (`subjectChip` returns null), a time that wraps beside a wide chip, and the footer's messages
  still move the centred stage. This change removes only the Alarm row's share.
- The detection rows. They are absent only for an event with no `detection` field, which a
  current backend always sends (the feed calls `Store.events` with `with_subject` on). Against
  an older backend the panel shows the Alarm row alone, and that height difference is accepted.
- Live-updating the popup. The player's stats are not re-rendered on a feed reload today, and
  the popup's text is fixed at the moment it opens.
- The admin app.

## Design

All changes are in `compute/api/web/user/index.html`.

### The row

`renderPlayerStats(ev)` always emits the Alarm row first. Because the row is always present,
the branch that hides the panel when there is neither `detection` nor an alarm goes away: the
panel is always shown. The scroll reset on hop and the bottom-edge fade (`updateStatsFade`) stay
as they are. The three detection rows are unchanged.

- **Layout**: one element spanning both columns of the `.player-stats` grid
  (`grid-column: 1 / -1`), laid out as a single flex line:
  - the label "Alarm", styled like the other rows' `.k`, at the left edge;
  - the status text, right after the label;
  - the Read more button, pushed to the right edge.

  It is **not** a `.v` cell. `.v`'s `justify-self: end` makes a cell exactly as wide as its
  content, so a long status overflows across the label instead of being cut (measured). The
  status span takes `flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap`, the button takes `flex: none`, and the row stretches to the grid's full
  width.
- **No alarm** (`!hasAlarm(ev)`): the same element holds only the label. There is no status, no
  button, and no placeholder character.
- **Alarm**: "*state* · *verdict*", then Read more.
  - **State**, from `alarmState(ev)`: `sounding` gives "Sounding now", `sounded` gives
    "Sounded", `silenced` gives "Silent".
  - **Verdict**: "unknown cat" for `unknown`. For `neighbour`, the cat's name, falling back to
    "cat #id" as `alarmPhrase` does. If the visit's episodes don't all name the same cat (a
    different verdict, or a different `cat_id` for `neighbour`), it reads "mixed verdicts" rather
    than a count of cats, which could be one cat judged two ways. The line never shows how many
    episodes there were; the popup lists each one.
  - **Read more**: a `<button type="button">` styled as a text link in the row's font size. It
    has `aria-haspopup="dialog"` and the accessible name "Read more about the alarm". Its tap
    target may extend beyond its line box (padding cancelled by a negative margin, or a
    pseudo-element), but it must not change the row's height.
- The `.v.phrase` multi-line styling (`white-space: pre-line`) belongs to the popup now, not the
  row. The row's `title` is dropped and its sentence moves into the popup.
- **Height invariant**: the Alarm row is the same height in both states. Equivalently, for the
  same viewport, the panel's `scrollHeight` is identical for a visit with an alarm and one
  without (both with `detection`).

### The popup

A static element inside `#playerDialog`, after `.player-actions`. `.dialog` gains
`position: relative`, so the overlay covers the dialog (`position: absolute; inset: 0`) and
never the page. It takes `z-index: 2` or higher, because the filmstrip's `.film-conf` bar
(`z-index: 1`, in a tile that forms no stacking context) would otherwise paint over it.

- **Look**: a scrim dims the whole player, the head included. A card on top of it, using the
  dialog's surface, border and radius, contains:
  - the heading "Alarm";
  - one paragraph per episode, `alarmText(a, ev)`, in start order;
  - the muted explanation: "What the foreign-cat alarm did at this visit, and what it took the
    cat for. It decides on a few seconds of frames, so it can disagree with the name on the
    card.";
  - a Close button.

  The card is no taller than the dialog minus a margin, and a long text scrolls inside the
  card.
- **Semantics**: `role="dialog"`, `aria-modal="true"`, `aria-labelledby` pointing at its
  heading.
- **Open**: tapping Read more does the following, in order:
  - fills the card from the event currently shown;
  - pauses playback (`pausePlayback()`);
  - drops any hop waiting on a page fetch (`navPendingHop = false`), so the landing page only
    updates the nav, as it does for a reader who has moved (`openEvent`);
  - sets `inert` on every other child of `#playerDialog`;
  - shows the popup and moves focus to its Close button.
- **Close**: the Close button; Esc; a tap on the scrim outside the card; or a tap on the
  player's backdrop (`modalEl`), which while the popup is open closes only the popup, not the
  player. Closing clears the `inert` attributes. Focus then returns to the row's Read more
  button if it is still in the document, otherwise to `#playerDialog`.
- **Forced close**: `closePlayer()` and `openEvent()` both close the popup first. This is a
  safety net, since opening already dropped any waiting hop. If focus was inside the popup, it
  moves to `#playerDialog`, never left on a hidden button.
- **While open**, the document keydown handler checks the popup before anything else:
  - Esc closes the popup, calls `preventDefault`, and returns.
  - Tab and Shift+Tab call `preventDefault` and keep focus on the Close button, the popup's only
    control. `inert` covers only `#playerDialog`'s other children. The page behind the player is
    neither inert nor focus-trapped, since the player has no Tab trap of its own, so without this
    branch Tab walks out to the feed.
  - Any other key returns before the player's Space and arrow handling, **without**
    `preventDefault`, so Space still presses the focused Close button and the arrows scroll the
    card.

  `inert` stops the dialog's other controls from being focused or activated. The scrim covers
  the stage, so the swipe's touch handlers cannot fire, and it covers ×, Play, scrub, the
  filmstrip and the visit arrows.
- **A stats re-render while open** (the Analyse job's `stillOpen()` path at :2224) leaves the
  popup as it is. Focus return on close falls back as above if its button was replaced.
- **Hidden**: a qualified rule (e.g. `.alarm-pop.hidden { display: none; }`). A bare `.hidden`
  toggle does nothing on this page.

### Docs

`docs/ARCHITECTURE.md`, in the *Dashboard + API* row: "its player an *Alarm* line (how long, …
when the two differ)" becomes an Alarm row that is always there. It is blank for a visit with
no alarm; otherwise it is one line of what the alarm did and what it took the cat for, and
*Read more* opens the whole account (how long, how many clips, … when the two differ). Nothing
in `CONCEPT.md` changes.

## Alternatives considered

- **"Read more" expands in place.** This brings back the jump the change exists to remove.
- **Reserve room for the full text** (a fixed two or three lines). It wastes height on most
  visits, and a multi-episode explained alarm still overflows it.
- **Short line = today's full text cut to one line, "Status word only", or "Read more" alone.**
  These were offered and the user chose status plus verdict. The full text cut to one line
  breaks mid-phrase at a width-dependent point. The other two hide the verdict that entry 611
  put first for being the most useful line.
- **Fitting the verdict in the value column.** Measured in review, the column leaves about
  139 px at 390 px, so "Sounded, judged an unknown cat" read "Sounded, judged an…". Two other
  fixes were rejected for the full-width row: a "›" in place of "Read more", which loses the
  words, and putting the verdict first, which still truncates and only changes what is lost.
- **Anchor the dialog to the top** (`align-items: flex-start`). The stage would then ignore
  every height change below it. Rejected: the footer's visit arrows, tapped on every hop, would
  move by the full height difference instead of half of it.
- **Native `<dialog>` / Popover API.** A second modal mechanism on a one-pattern page, an Esc
  guard still needed, and iOS 15.4+ (Popover: 17+) for no visible gain.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Sonnet 5.5.** One file and one code path: the row, the popup and the key
  guard all depend on one another. The design is settled down to the CSS rules, so this is
  transcription plus measuring in a real browser, not interpretation. Most of the effort is
  the Playwright pass at 390 and 360 px.

## Amendments

- **2026-11-16** — The popup's key handler now holds Tab and Shift+Tab on Close, with
  `preventDefault`. `inert` on the dialog's other children never kept Tab in, because the page
  behind the player isn't inert and the player has no Tab trap: in Chromium, Tab reached the
  feed. Found in implementation.

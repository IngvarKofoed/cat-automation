# Visit-to-visit navigation in the user playback modal

Make the household able to open the newest visit and walk backwards and forwards
through visits — checking each identification label against the frames — with one
thumb on a phone. The existing Prev/Next buttons are *moved* out of the
frame-controls row into the actions footer beside "Mark for labelling", where the
other visit-level control lives, and a horizontal swipe on the played frame does
the same hop. No backend change; user page only.

## Key decisions

- **Visit nav moves, it isn't added** (extends). `playerPrev` / `playerNext`
  already exist at `compute/api/web/user/index.html:800-801` and already call
  `moveEvent(±1)`. Only their DOM home changes — from `.player-controls`
  (Play · scrub · frame count) to `.player-actions` (the flag). They read as
  *frame* stepping today purely because of where they sit, and they crush the
  scrub slider to ~40px on a 375px phone. The hop and bounds logic in
  `moveEvent` / `updatePlayerNav` / `openEvent` is unchanged; each gains only a
  readout or note to write.
- **Flag left, nav right** (new). The footer becomes
  `[Mark for labelling] [msg] … [‹ Newer] [12 / 40] [Older ›]`. The nav is one
  wrapper element so it wraps as a unit; below 480px it takes its own full-width
  line with `justify-content: space-between`, putting the two arrows at the
  screen's edges where a thumb reaches them. Right-alignment comes from `flex: 1`
  on the *message*, never a bare spacer: `flex-wrap` line-breaking uses each
  item's base size, so a full-width note would bump the nav onto its own line and
  left-align it. Same physics as `admin-next`'s right rail, re-derived here (no
  shared CSS).
- **Swipe is touch-only, on `.stage`, left = older** (new). `touchstart/move/end`
  on the frame area — not pointer events, so a desktop mouse drag keeps today's
  behaviour and the backdrop-close mousedown logic is unaffected. Swiping *the
  picture* is the unambiguous surface; the filmstrip below already owns horizontal
  scrolling. Content leaves to the left and the next visit arrives from the right,
  matching `Older ›` — and `activityVisible` is newest-first, so "older" is
  `moveEvent(+1)`, further down the feed.
- **The frame follows the finger** (new). `.player-frame` is `translateX`-ed
  during the drag and eases back only when the swipe *doesn't* move the visit (a
  short drag, or a refusal at either end) — a committed hop drops the transform
  instantly, since animating it back would read as the gesture bouncing off. So
  the gesture is self-teaching: you feel it engage before you release. At either
  end of the list the follow is damped to a third — a physical "nothing that way"
  that lands alongside the readout's wording.
- **The browser keeps its own gestures** (new). No `touch-action` declaration: the
  stage has no scroller of its own, so the default already permits vertical pan
  *and* pinch-zoom — and zooming into a small top-down cat is exactly what a
  household member wants from this frame. Instead the axis latch calls
  `preventDefault` only once a drag has committed to horizontal. A gesture
  starting within 24px of the viewport's left edge is ignored outright, so
  Safari's interactive back-swipe survives (the page is served in-browser as well
  as installed to the home screen, entry 88; only the latter has no edge-back —
  and `pan-y` may suppress edge-back itself, which would have made the dead zone
  pointless).
- **End-of-list feedback rides the position readout** (new). A refused swipe
  appends to `#playerVisitPos` — `40 / 40 · oldest loaded` — rather than getting
  its own element. That readout is already reset by `closePlayer`, is never
  written by the flag path, and carries no live region, so the message needs no
  lifecycle of its own. Deliberately *not* `#playerFlagMsg`: the flag write
  reports there when its POST settles, so it would clobber a nav note at an
  arbitrary moment.
- **The position readout honours `truncated`** (extends). `/api/events` returns
  `{events, truncated}` and the user page currently drops the flag
  (`edata.events || []`). Carry it, and render `12 / 40+` when the feed was
  scan-capped — asserting a bare total the backend told us is incomplete is the
  same falsehood the end-of-list note exists to avoid. `admin-next`'s annotation
  page already renders exactly this `${i+1} / ${n}${truncated ? '+' : ''}`;
  duplicated here, not shared (entry 80).
- **The stage yields height; the dialog never scrolls** (extends). `.dialog` is
  `overflow: hidden` with `max-height: 94vh`, so the ~40px the new footer line
  adds is *clipped* on a short viewport, not scrollable. Rather than making the
  dialog a scroll container — which would let the × and the nav scroll out of
  view, exactly where the nav is most needed — `.stage` gains `min-height: 0` so
  it becomes the column's shrinking item and the 4/3 frame gives up height first,
  letterboxing inside (the contained image already does this on a wide dialog).
  Header and footer stay permanently reachable, and `overflow: hidden` is
  untouched.
- **User page only** (reuses). Per the entry-80 convention the two front doors
  share no CSS or JS. `/admin`'s Activity player keeps its own Prev/Next as-is;
  this does not become a shared helper.

## Goals

- Step visit → earlier visit → back again from inside playback, by button or by
  swipe, without leaving the modal — with the swipe visibly engaging as you drag,
  so it is discoverable and can't be mistaken for a glitch.
- Make it legible that the control moves between **visits**, not frames.
- Give the scrub slider room to be usable on a phone.
- Keep an audit run orientable: always know which visit of how many you're on.

## Non-goals

- Loading visits beyond what `/api/events` returned for the feed. The endpoint
  *does* take `since_id` / `until_id` (that is how `/admin` windows it), so
  paging older visits in is buildable — deliberately out of scope here. The nav
  walks the loaded, filtered list only, and says so at the end of it.
- Swipe gestures anywhere else — not the Activity feed (it scrolls vertically),
  and no vertical swipe-to-dismiss.
- Parity in `/admin`.
- Any change to what a visit is, to the frame-level controls' behaviour, or to
  what the user app can write. The flag stays the only write from here.

## Design

### Footer markup

`.player-actions` gains a nav group after the existing flag button and message:

```html
<div class="visit-nav">
    <button class="btn ghost" id="playerPrev" disabled aria-label="Newer visit">
        <span aria-hidden="true">&lsaquo;</span> Newer</button>
    <span class="visit-pos mono" id="playerVisitPos">—</span>
    <button class="btn ghost" id="playerNext" disabled aria-label="Older visit">
        Older <span aria-hidden="true">&rsaquo;</span></button>
</div>
```

`.visit-nav { display: flex; align-items: center; gap: .5rem; margin-left: auto }`,
and under `@media (max-width: 480px)`, `flex: 1 1 100%; justify-content:
space-between; margin-left: 0`. `#playerFlagMsg` gets `flex: 1; min-width: 0`.

Below 480px `.visit-pos` is also `flex: 1; text-align: center`, so the readout
absorbs its own width changes — the end-of-list suffix appears and disappears, and
without this both arrows would shift under a `space-between` layout every time it
did.

`.visit-pos` copies the `.player-controls .count` treatment — mono,
tabular-nums, muted — so the two readouts read as the same kind of thing at
different scales (frame within visit, visit within run). It needs its own rule:
that declaration is *scoped* to `.player-controls`, so it doesn't reach the
footer. Same for the compact `.player-controls .btn { padding: .4rem .8rem }` —
the moved buttons must carry that padding explicitly under `.visit-nav`, or they
land at the default `.5rem 1rem` and the 480px one-line layout no longer fits.

### Position readout and `updatePlayerNav`

`updatePlayerNav()` already derives both buttons' `disabled` from
`activitySelectedIndex`; it additionally writes
`` `${activitySelectedIndex + 1} / ${activityVisible.length}${activityTruncated ? '+' : ''}` ``
into `#playerVisitPos` (`—` when nothing is selected). `closePlayer()` resets it.
`loadRecentActivity` stores `edata.truncated` into a new `activityTruncated`
alongside `activityEvents`.

The readout is plain text, not an `aria-live` region — a live region firing on
every hop, beside the flag's own, would be noise. `openEvent` instead sets the
dialog's `aria-label` to the facts the rail rows carry (`setRowLabel`'s shape):
time, subject, position.

**Known limit, stated rather than solved:** that re-label is *not* reliably
announced on an already-open `aria-modal` dialog, and focus stays on the pressed
button — so a keyboard/screen-reader user gets no spoken confirmation that the
visit changed. Accepted for now; the fixes if it matters later are moving focus
into the dialog on hop, or making the position readout `aria-live="polite"`.
The `&lsaquo;` / `&rsaquo;` glyphs *are* wrapped in `aria-hidden` spans, with the
direction carried by each button's `aria-label` — unwrapped, a screen reader says
"single left-pointing angle quotation mark".

### Stage height

`.stage` gains `min-height: 0`, and nothing else. A flex item's default
`min-height: auto` refuses to shrink below its `aspect-ratio`-derived height,
which is what made the footer overflow `max-height: 94vh` and get clipped;
zeroing it makes the stage the column's shrinking item. The inner `.player-frame`
is already `max-width/height: 100%` inside a `place-items: center` grid, so the
image letterboxes rather than overflowing — the same path a wide dialog already
takes. `.dialog` keeps `overflow: hidden`, so nothing in the modal ever scrolls
and the × and the nav are always on screen.

Deliberately *not* a `max-height` in `vh`: that hardcodes an assumption about the
chrome's height, and any value low enough to help a 568px-tall phone also shrinks
the frame on a tall desktop. Flex shrink gives up exactly the height the viewport
demands and none otherwise — measured at 390×664, the stage yields 279→225px and
the dialog does not overflow; at 1100×900 it yields 6px.

### Gesture

State: `touchStartX`, `touchStartY`, `touchActive`, `touchAxis`
(`null` | `'x'` | `'y'`).

- `touchstart` — ignore unless `touches.length === 1`; ignore if `clientX < 24`
  (edge-back dead zone). Record the origin, set `touchActive`, `touchAxis = null`.
- **Any** touch event seeing `touches.length > 1` clears `touchActive` and resets
  the transform. This is load-bearing, not defensive: a second finger fires its
  own `touchstart`, and merely *ignoring* that one leaves the first gesture armed
  — so a pinch would end with a large `dx` and hop to another visit. Clearing on
  the second finger is what lets pinch-zoom and the swipe share the stage.
- `touchmove` — registered `{ passive: false }`, because the horizontal branch
  calls `preventDefault`. While `touchAxis` is null, latch it once past a 12px
  deadzone: `|dx| > |dy|` → `'x'`, else `'y'`. Then:
  - `'y'` — do nothing, ever. The browser keeps the gesture (page pan, and pinch
    if a second finger arrives).
  - `'x'` — `preventDefault()` and set `.player-frame`'s
    `transform: translateX(<dx>px)`. If the hop that `dx` implies is out of
    bounds, damp it to `dx / 3` — a rubber-band that says "nothing that way".
- `touchend` — the lifted finger is in `changedTouches[0]`, not `touches` (empty
  by then). Only when `touchAxis === 'x'`: commit if `|dx| >= 45`. `dx < 0` →
  `moveEvent(+1)` (older), `dx > 0` → `moveEvent(-1)` (newer). No time limit — a
  slow deliberate drag is still a swipe. Either way clear the transform.
- `touchcancel` — clear `touchActive` and the transform.

The axis latch replaces the earlier `|dx| > 1.5 * |dy|` release-time test: with a
drag-follow, the decision has to be made *during* the gesture (to know whether to
`preventDefault`), and making it once and sticking to it is what stops a wobbly
thumb flickering between panning and dragging.

**Snap-back** is a `transform .18s ease-out` transition on `.player-frame`, added
only for the release so the drag itself stays 1:1 with the finger. It is the
"that didn't take" cue, so it plays **only when the frame isn't about to be
replaced** — a short drag, a refusal at either end of the list, or a cancel. A
committed hop drops the transform instantly instead; easing it back to centre
first would read as the gesture bouncing off the very swipe that worked.

The existing `prefers-reduced-motion` block already zeroes all transitions, and
the transform is cleared by assignment rather than by relying on the animation, so
a reduced-motion user gets an instant reset, not a stuck offset.

A committed swipe goes through `moveEvent`, so it inherits everything the button
path has: bounds checks, `openEvent`'s `playerSeq` staleness guard, autoplay from
frame 0, and the flag button re-pointing at the new event. `moveEvent`'s existing
out-of-bounds early return is where the end-of-list wording is set (below) — the
buttons are disabled at the ends, so a swipe is the only caller that reaches it.

Because the transform lives on `.player-frame`, the YOLO detection box travels
with the image it belongs to; the box is positioned in percentages of that same
wrapper, so no overlay math changes.

### What already holds

- **Nav walks the filtered subset.** `activityVisible` is what "Hide our cats" /
  "Show all" re-slice, so navigation can never land on a hidden visit
  (entry 78). For a label audit you'd leave "Hide our cats" *off* — checking
  residents is the point.
- **The list can't shift mid-run.** `refreshActiveView()` returns early while the
  player is open, and both filter toggles call `closePlayer()` before
  re-rendering — so `activitySelectedIndex` stays valid for as long as a
  swipe session lasts.
- **Keyboard is unchanged.** `ArrowLeft`/`ArrowRight` stay frame-level scrubbing;
  the nav buttons are reachable by Tab and Enter like any button. No new
  shortcut — the ask is mobile, and taking the arrow keys for visits would break
  the frame stepping an audit also needs.

### End of the list

`Older ›` disables at the last loaded visit, `‹ Newer` at the first. A *swipe*
past either end gets two cues: the damped rubber-band during the drag, and a
suffix on the position readout written by `moveEvent`'s out-of-bounds early
return.

The suffix must not assert an absolute that is false, and there are **two**
independent ways it can be — so it hedges rather than always claiming, and rather
than always hedging:

| state | newest end | oldest end |
| --- | --- | --- |
| a toggle is hiding events | `· newest shown` | `· oldest shown` |
| feed complete, nothing hidden | `· newest` | `· oldest` |
| `truncated`, nothing hidden | `· newest` | `· oldest loaded` |

The filter case is the **common** one, not an edge: `showAll` defaults off, so the
noise kinds are hidden unless asked for, and nav walks the filtered list — a
hidden newer event makes a bare "newest" a lie on the default view. "shown"
subsumes a capped feed, so it wins over "loaded" when both apply. Conversely,
hedging about a scan cap that didn't apply is equally wrong, which is why the
unfiltered complete case says plain "oldest".

No lifecycle to manage: the suffix is part of what `updatePlayerNav` writes, so
the next hop overwrites it and `closePlayer` already resets the readout.

### No prefetch

A hop fires one `/api/frames/sample` and blanks the stage with "Loading frames…"
for a beat. That stands: it is a LAN request, `playerSeq` already discards a
stale response, and prefetching the adjacent visit would spend requests on hops
that never happen. If a swipe run turns out to feel laggy in practice, the
annotation page's prefetch pattern (entry 217) is the known fix — deliberately
not built up front.

## Alternatives considered

- **Swipe on the whole dialog** rather than the stage. More forgiving — the thumb
  needn't be on the image — but a swipe starting on a button is ambiguous, the
  filmstrip's own horizontal scroll has to be excluded, and every control added
  later inherits that exclusion burden.
- **Scroll-snap pager**: three slides (prev/current/next visit) in the stage, so
  swipe is native scroll-snap with momentum and animation. Much the nicest
  gesture, but it needs neighbours' frames fetched up front (3× the sample
  traffic) and the box overlay, filmstrip and stats are all per-visit — they'd
  have to follow the snap or visibly lag it.
- **Leaving Prev/Next where they are and adding a second pair** in the footer.
  Two controls doing one thing, and the scrub slider stays crushed.
- **`touch-action: pan-y` on the stage** to claim the horizontal axis
  declaratively. Rejected: it disables pinch-zoom on the one image worth zooming,
  and the conditional `preventDefault` in the axis latch already achieves the same
  thing without giving that up.
- **A dedicated `#playerNavMsg` element** for the end-of-list message, and
  **making the dialog a scroll container** for the taller footer. Both rejected as
  more machinery than the job needs — the position readout already has the right
  lifecycle, and a shrinking stage keeps the nav on screen instead of scrolling it
  away.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** One file
  (`compute/api/web/user/index.html`), and the markup, the CSS and the gesture
  handler are the same code path — the axis latch, the `preventDefault` decision
  and the `translateX` can't be written apart from each other.
- Verification is the compute subtree's dashboard workflow: drive the modal in a
  real browser via the Playwright MCP, at a phone viewport, and check the console
  and network tabs. The gesture needs `browser_drag` (or dispatched touch events)
  at both list ends and mid-list.

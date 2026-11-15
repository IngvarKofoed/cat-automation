# User player: the frame keeps its size

On the iPhone, the user app's player sometimes draws the frame much smaller than its
stage. It happens on whichever frames playback reached before they had loaded, which is
why a reopen (now served from cache) shrinks fewer. Two things combine:

- The frame's box takes its width from the picture. `.player-frame`
  (`compute/api/web/user/index.html:855`) is a centred grid item with `width: auto`.
- Playback swaps `src` every 200 ms whether the next frame has arrived or not
  (`showPlayerFrame`, :3154, driven by `startPlayback`'s tick).

A second, separate squeeze: the stage is the dialog column's main shrinking item
(entry 253); the filmstrip gives up a little too. So the Alarm row (entries 549, 602) and the head wrap the ⬇ button can
cause (542) are taken out of the frame's height on a phone, and on alarm visits the
stage drops below the frame's natural size.

This spec fixes both:

- Size the frame from the stage, never from the picture.
- Let playback advance only to a frame that has loaded.
- Make the stats block give up height before the stage does.

## Outcome

**What you get:**

- Playing a visit on the iPhone, every frame fills the stage. A frame that hasn't
  arrived makes playback pause on the current one instead of drawing a smaller one.
- Opening a visit, or hopping between visits, never shows a collapsed frame before the
  first picture arrives.
- On a phone, an alarm visit's frame is as large as an ordinary visit's. The Alarm line
  comes first in the stats, and the rest of the stats scroll.

**How to verify:**

- **On the iPhone** (the reported bug, and checkable only there): open a visit you
  haven't opened before and press Play. No frame draws smaller than the others.
- **In Chromium, with `/media` responses delayed ~1 s each:**
  - On open or hop, the frame area is full size before the first picture arrives (today
    it is 103×66).
  - During playback, the count never moves past the picture on screen, and the box
    always sits on the shown frame.
  - A frame whose `/media` request is blocked shows an empty stage at full size for its
    turn, and playback continues.
- **At 390×750 on an alarm visit with the explanation sentence:**
  - The frame is 372×240, the same as on an ordinary visit, and nothing overlaps
    Play/scrub.
  - The Alarm row is the first stats line, and the stats scroll. A fade shows at their
    bottom edge until they are scrolled to the end.
  - At 390×568, the footer nav is still fully on screen.

## Key decisions

- **The frame is sized from the stage** (new). `.stage` becomes a size container, and
  `.player-frame`'s width is `min(stage width, stage height × r)`, with `aspect-ratio: r`.
  This is the first container-query use in either dashboard.
  - `r` starts at 4/3 and is set from the first loaded frame's natural size by the
    existing `load` listener (:2968). That listener pins `aspect-ratio` today; it now sets
    the value the width formula reads as well.
  - The `max-width / max-height: 100%` pair goes. It never clamped: in Chromium the frame
    overflowed a short stage instead of letterboxing (measured).
- **The Play tick holds; nothing else does** (diverges). A tick advances only when the
  next frame's preloaded `Image` is `complete`, meaning loaded or failed. Otherwise the
  tick does nothing and the next tick checks again. Playback becomes a stutter on a slow
  connection, never a dark flash or a skipped frame. Reviewing a visit is the point of
  the player.
  - Scrub, tile tap and arrow keys still jump straight to their frame, as today. On a
    slow link a jump can show a dark stage, or the previous frame's box, until the
    picture lands. It never shows a smaller frame.
  - Chosen over making every control wait (decided at review). That needed a
    requested-vs-shown split and latest-request-wins, which was easy to get subtly wrong,
    for a case nobody reported.
- **A failed frame shows as an empty stage at full size** (new). `complete` covers
  failure, so playback moves on. For its turn the stage is empty and no box is drawn.
- **No bound on a hold** (accepted, decided at review). A frame whose request never
  completes holds playback on the previous frame until it does, with the button still
  reading Pause. The reader can scrub or tap past it, since manual controls don't wait.
- **The stats give way before the frame** (extends entry 253).
  - `.player-stats` becomes a vertical scroll area that absorbs any squeeze, down to a
    floor of two lines of text (~3rem). That is lines, not grid rows: the Alarm row
    alone wraps to ~4 lines on a 390 px phone, so a two-row floor would be ~125 px and
    give the frame back too little.
  - Past that floor the stage shrinks, still as the last resort, so the footer nav stays
    reachable at any height (253's reason).
  - The filmstrip stops shrinking. Today a squeeze crops its tiles and gives it a vertical
    scrollbar.
- **The Alarm row comes first in the stats** (diverges). At the floor, only the first
  lines show without scrolling. The alarm's verdict on an alarm visit matters more to the
  reader than three detector percentages.
- **A fade marks stats that continue below** (new). While the stats area has content
  below its visible part, its bottom edge fades out; scrolled to the end, the fade goes.
  iOS hides scrollbars at rest, so without it a cut-off Alarm sentence reads as the
  whole sentence.

## Goals

- A frame that has not loaded never makes the frame smaller, in any browser.
- Playback on a slow connection waits for each frame rather than flashing an empty stage.
- On a phone, an alarm visit's frame is as large as an ordinary visit's, and nothing
  overlaps the Play/scrub row.
- The footer nav stays on screen at any viewport height (entry 253, kept).

## Non-goals

- **Admin's players** (Activity, and Annotation's player from entry 291). They live in a
  separate file, the bug wasn't reported there, and they were not checked.
- **Faster frame delivery.** The preload window (`PRELOAD_AHEAD` = 16), the 200 ms
  cadence, `/media` caching and compute-PC load stay as they are. With this fix, slow
  delivery becomes a stutter rather than a shrink.
- **The stage's 4/3 ratio.** Frames are 1076×694 (≈1.55), so a phone keeps ~39 px of
  letterbox bands. Matching the stage to the frame would reflow it when the first frame
  loads, which is the jump the fixed stage exists to prevent.
- **A buffering indicator.** A hold reads as a stutter, and the frame doesn't move.
- **The Alarm row's wording,** and the download clip.
- **A phone in landscape** (~350 px tall). It leaves the stage almost no room, today
  and after. The player is portrait-first.
- **Closing the player while its frame list is still loading.** `closePlayer` never
  bumps `playerSeq`, so `openEvent` still runs `startPlayback()` (:3104) behind the
  hidden modal. This is pre-existing and not touched here.

## Design

### Why the frame shrinks

`showPlayerFrame` assigns `playerImgEl.src` directly. The `<img>` sits in
`.player-frame`, whose width is shrink-to-fit from the image's intrinsic size. So the box
is the picture's size, capped by the stage.

Chromium keeps showing the previous picture while a new `src` loads. Even there the box
collapses whenever no picture is present: after `openEvent` clears `src` (:3076) on
open or hop, the frame measures 103×66 (the alt text) until the first frame lands.

WebKit evidently doesn't hold the old picture through a `src` change. That is inferred
from the symptom: no WebKit was available to run. So on the iPhone the same collapse
happens on every frame that playback reaches early.

None of this code changed in the last three weeks (it dates from 22–24 July). What
changed is when frames arrive, and that hasn't been measured from the phone.

### Sizing the frame from the stage

- **`.stage`:** gets `container-type: size`. Its size already comes from the dialog's
  width, its 4/3 ratio and the flex column, never from its content, so containment
  changes nothing about the stage itself.
- **`.player-frame`:** gets `width: min(100cqw, 100cqh * r)` and `aspect-ratio: r`, with
  `r` a custom property defaulting to 4/3. It is still centred by the stage's grid.
- **The `<img>` is hidden while it has no picture:** when `src` is removed and on its
  `error` event. It is shown again on its next `load`. At full size, an empty `<img>` would draw its alt text
  ("Event frame") on the dark stage.
- **Unchanged:** the `<img>` (`object-fit: contain`), the box overlay (percentages of
  the natural size, `positionBbox`, :2947), and swipe drag-follow (a transform on the
  frame).

What this changes for the reader:
- **On open or hop:** the frame is full size before any picture arrives, as a dark area
  of the right shape.
- **When the stage does shrink** (a very short screen): the frame letterboxes inside it.
  That is what 253's comment always claimed and the CSS never did.

### Holding playback

Only the Play tick changes (:3188 inside `startPlayback`). `showPlayerFrame` and its
other callers stay as they are: the first frame (:3101), a tile tap (:3137), the scrub
(:3377) and the arrow keys (:3401–3402).

- **The tick:** computes `next` as today, from `playerIndex`, wrapping to 0.
  - If `playerImages[next]` exists and is `complete`, the tick calls
    `showPlayerFrame(next)`.
  - Otherwise it does nothing and the next tick checks again.
  - `showPlayerFrame` already preloads 16 ahead of the current index, so the frame being
    waited on is always requested.
- **A failed frame** is `complete` with `naturalWidth` 0, so the tick moves to it and
  then past it. Showing a frame that failed draws no box: `positionBbox` is skipped, or
  the box hidden, when that frame's `Image` failed. The `<img>`'s own `error` hides the
  picture (above).
- **The tick doesn't wait for the current frame.** On open, frame 0 is set directly. If
  frame 1 arrives first, playback may move on before frame 0 is drawn. That is harmless,
  and simpler than a second condition.

A tick swaps to an image the browser already holds, so it should draw with no gap. If
WebKit still blanks for one paint, the box no longer shrinks, so the worst case is a
one-frame dark flash. That can only be confirmed on the iPhone.

### The stats give way first

- **`.player-stats`:** `overflow-y: auto`, a `min-height` of two lines (~3rem), and a shrink
  weight far above the stage's. Flex shrinks in proportion to weight × base size, so a
  weight ratio of ~1000 makes the order effectively strict. Any mechanism that gives the
  same order is fine.
- **`.filmstrip`:** `flex: none`.
- **`.stage`:** keeps `min-height: 0` and is the only other item that shrinks.
- **Hop:** the stats area's scroll position resets to the top on every hop.
- **Fade:** a class on `.player-stats` draws a soft fade over its bottom edge (a mask or
  gradient, ~1.5rem). It is set while `scrollTop + clientHeight < scrollHeight` (with a
  pixel of slack), and re-evaluated on the area's `scroll`, after every
  `renderPlayerStats`, and on resize. It sits in the stats area's own space, so it never
  draws over the filmstrip or footer.
- **No gesture conflict:** the stats scroll on their own, and the swipe gesture lives on
  the stage.
- **`renderPlayerStats`** (:2986) emits the Alarm row before the detection rows. When
  the detection rows are absent, nothing else changes.

Expected at 390×750 on an explained alarm visit, from today's measurements:
- Today the stats are ~179 px and the stage ~173 px, below the frame's natural 240 px.
- At the ~50 px floor, the stats give back ~130 px. The filmstrip, now `flex: none`,
  takes back the ~25 px it is squeezed by today. The stage needs ~67 px to reach 240,
  so the frame gets its full 372×240 with room to spare.
- At 390×664 the stage reaches ~207 px, so the frame keeps about 86% of its width;
  today it keeps 43%.

## Alternatives considered

- **Keep real time and skip unloaded frames.** Smooth, but frames go missing, and a cat
  that's in view only briefly can drop out of the playback. Rejected: the player is for
  reviewing a visit.
- **Every control waits for its frame.** The picture, box and count would always agree,
  but it needs a requested-vs-shown split and latest-request-wins, which the review found
  easy to get subtly wrong. Rejected at review, since only playback was reported.
- **Give up on a stuck frame after a few seconds.** Rejected at review: manual controls
  still reach past it.
- **Two stacked images as a double buffer.** This would hide WebKit's blank paint, but
  the box would still need sizing from the stage. Swapping to an already-loaded image
  gets most of the benefit with the one element.
- **The stage takes the frame's real ratio,** reclaiming ~39 px of letterbox bands for
  the stats. Rejected at review: everything below the frame would jump once per page
  load, and the fade already says there is more.
- **Truncate the Alarm row to one line, tap for the rest.** This fixes one row only; the
  next row added to the player squeezes the frame again.
- **Let the whole dialog scroll under a pinned footer.** Simplest CSS, but reading the
  stats scrolls the frame and Play/scrub out of view.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** It is all one file (`compute/api/web/user/index.html`): a
  few CSS rules, the Play tick, the `<img>` show/hide, the stats order and the fade. The
  pieces share the modal's flex column, so they have to be measured together, in a
  browser at 390×750, 390×664 and 390×568 with `/media` delayed. Parallel agents would
  only collide in the same CSS block.

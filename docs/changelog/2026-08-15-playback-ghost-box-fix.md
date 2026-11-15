# Admin playback no longer draws a stale detection box

<!-- entry 156 -->

admin-next playback no longer draws a GHOST detection box: `show()` cleared `fb` per frame but
left `img.onload` pointing at the previous frame's placement closure, so a box-less frame — which
never reassigns it — had the stale handler fire on its own load and paint the prior frame's box
under a "no detection" caption. `onload` is now reset before each `src` swap.
Also: the annotation rep-frame box lost its colour when `.fbox.warn` was replaced by the
`tl-*` traffic light; it now bands by the rep detection's own score and reuses `placeBoxes`.
That score is shown as "rep %" beside the visit's "peak %" — the rep can sit far below the
peak, so otherwise the border colour is a number the operator has no way to read.

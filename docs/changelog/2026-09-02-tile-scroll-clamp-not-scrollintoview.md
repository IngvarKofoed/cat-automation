# Selected tile scrolls by clamping the grid's scrollTop

<!-- entry 247 -->

The selected tile is scrolled into view by clamping the grid's own `scrollTop`, never by
`scrollIntoView`, which walks every scrollable ancestor: with the grid scrolled above the
viewport each Prev/Next hop scrolled the PAGE up by ~84px, yanking away the detail stage
being read. Measured, not theorised — the prior comment claimed the opposite.

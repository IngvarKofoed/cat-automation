# Stage min-height 0 so the footer nav is not clipped

<!-- entry 253 -->

`.stage` gained `min-height: 0` so it is the dialog column's shrinking item. A flex item's
default `min-height: auto` won't shrink below its aspect-ratio height, so the new footer line
overflowed `max-height: 94vh` and `overflow: hidden` CLIPPED the nav — the one control that
must stay reachable. Not a `vh` cap: any value low enough to save a 568px phone also shrinks
the frame on a tall desktop.

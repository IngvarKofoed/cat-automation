# Frame letterboxes and hides an img with no picture

<!-- entry 607 -->

The old `max-width/max-height: 100%` pair never clamped: a short stage was OVERFLOWED onto
Play/scrub rather than letterboxed (measured). The frame letterboxes now. An `<img>` with no
picture (src removed, or a failed load) is `visibility: hidden` until its next `load`, or the
full-size frame would draw "Event frame" on the dark stage.

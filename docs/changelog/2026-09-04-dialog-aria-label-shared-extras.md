# Playback dialog aria-label shares the feed's extras

<!-- entry 255 -->

The playback dialog's `aria-label` carries the same health + flag sentences the feed rows do,
via one shared `visitAriaExtras` so the two can't claim different things about one visit. The
flag fragment is elsewhere called the ONLY signal a visit is marked for a screen reader, and
the dialog is where it gets toggled.
Known limit: a hop is still silent to a screen reader — re-labelling an already-open
`aria-modal` dialog isn't reliably announced and focus stays on the pressed button.

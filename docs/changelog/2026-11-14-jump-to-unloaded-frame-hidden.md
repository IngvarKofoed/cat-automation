# Jumping to an unloaded frame hides the img until it loads

<!-- entry 612 -->

Review repair: a scrub, tile tap or arrow to a frame with no picture yet — still loading, or
failed — hides the `<img>` and its box until that frame's own `load`, as open/hop already did.
Only the Play tick waits, so a jump past the preload drew the alt text across the full-size
frame on WebKit (a dark stage now, as the spec intended), and a failed frame flashed the
broken-image glyph. Without container units (Safari < 16) the frame fills the stage's width.

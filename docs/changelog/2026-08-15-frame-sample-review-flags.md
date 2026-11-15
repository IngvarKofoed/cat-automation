# Frame sampling can attach motion and corrupt markers per frame

<!-- entry 157 -->

`GET /api/frames/sample?flags=1` attaches the two per-frame REVIEW markers a grid outlines tiles by:
`motion` (off `frames.motion`) and a TRI-STATE `corrupt` — True/False from a stored `corruption`
verdict, `None` where no corruption sweep has reached the frame. `None` is deliberately not False:
showing unmeasured as clean is the "an empty danger set reads as safe" trap of entry 97.
Additive — without the flag the payload is byte-identical.

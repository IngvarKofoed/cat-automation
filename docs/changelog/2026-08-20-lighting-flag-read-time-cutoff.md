# Lighting flag stores colourfulness, cutoff applied at read time

<!-- entry 180 -->

Day/night LIGHTING flag: a non-registered `lighting` sweep stores a continuous
colourfulness statistic per frame (`analysis.score`), and the day/night cutoff is applied
at READ time from the settings KV — so it can be swept NOW, before the NoIR camera, and
calibrated afterwards from the recorded distribution with no re-sweep.
Measured-but-uncalibrated reads `day` + `calibrated:false`; an UNSWEPT frame stays null —
a sweep that never ran must not present as a measurement.
Per-channel mean normalisation makes it blind to a constant cast, which a locked white
balance leaves on every IR frame; the downscale is its OWN constant, never
`motion_downscale`, so a MOG2 retune can't silently invalidate a saved cutoff.
Spec: docs/specs/2026-07-27-lighting-flag.md.

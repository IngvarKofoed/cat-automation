# Luma separates day from night, colourfulness cannot

<!-- entry 273 -->

Measured over 30 July (707,784 frames, full lighting coverage): colourfulness CANNOT
separate the regimes at this door. Evening daylight reads 0.088–0.100, at or below IR
night's 0.100–0.103 — the scene is achromatic in daylight too, so the statistic tracks
direct sun rather than colour-vs-IR. Mean luma does separate cleanly (night 45.6–47.8,
day 83.4–98.6) and is ALREADY stored in `analysis.detail`, so switching axis later
would need no re-sweep. The separation depends on the lamp staying dim: auto-exposure
is railed at night, so a brighter emitter would erode the gap.

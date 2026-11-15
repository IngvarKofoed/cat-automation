# Scorecard readouts are one line per stat

<!-- entry 177 -->

A scorecard readout is now ONE LINE per stat — label left, value right, the same shape as the
param list under it. Laid out as a single wrapping flex row, every gap broke in a 210px tile,
so Recall/Missed/False/Day/Night ate ten lines with each label stranded above its number.
Verified at the grid's 210px minimum: no row wraps or overflows. Presentation only.

# swept counts rows over the whole span

<!-- entry 280 -->

`swept` counts rows over the WHOLE span, not its motion frames, matching what a
span-scoped sweep fills — so it can disagree with `ratio`'s motion-only denominator
(swept, yet "not measured"). Narrowing it to motion frames would call a span
unanalyzed that a sweep cannot add one verdict to.

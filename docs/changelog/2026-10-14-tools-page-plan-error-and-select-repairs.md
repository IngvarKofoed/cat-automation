# Tools page keeps plan errors and locks the shape select during jobs

<!-- entry 457 -->

Two Tools-page repairs. A failed plan's error was written and then overwritten by
"Planning…" in the same tick (no `return` before the shared re-render tail), so the
card stuck on a progress word with no hint the request failed. And the crop-shape
select is disabled while any job runs — changing it mid-run re-planned a different
target over a card whose job readout belonged to the running one, and the
post-completion re-plan reads the select, so it then described a shape that job never
moved to.

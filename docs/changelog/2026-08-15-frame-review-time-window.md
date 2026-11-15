# Frame review is scoped by a start instant and timespan

<!-- entry 154 -->

Frame review is scoped by a start instant + a 1/5/10/15/30/60-min timespan (the corruption
page's picker shape) instead of a whole day, so an operator can land on the minute a visit
happened. The frame-count field is gone; the window's own width bounds the grid.
It now reports `N of M loaded (sampled)` from the window's real frame total — the sample is
decimated above 500 and complete below it, and the response alone can't tell the two apart —
plus how much of the window YOLO has swept, which is what the "unswept" chips reflect.

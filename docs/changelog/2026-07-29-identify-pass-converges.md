# Identify pass marks unembeddable frames so progress reaches 100%

<!-- entry 71 -->

Identify pass now converges and counts truthfully: a detected frame it can't embed (no yolo-serial
box, or an undecodable/degenerate crop) gets a marker row (`cat_id` NULL, ignored at read) so it's
recorded processed and never re-attempted, and iter/count agree so progress reaches 100%.
`n_identified` counts only rows that actually persisted (frames evicted mid-pass excluded).

# Guard test asserts events is never called for an empty window

<!-- entry 353 -->

The `since_id is None` guard's test asserts `events` is NEVER CALLED, not that the
counts are zero. The per-event `start_ts < since_ts` filter zeroes those totals with
or without the guard, so the count-based test passed with the guard deleted — proven,
then re-proven failing after the repair. What the guard buys is not reading at all.

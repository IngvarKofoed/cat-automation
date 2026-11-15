# Clock-to-id resolution uses an indexed seek

<!-- entry 103 -->

`resolve_ts_range` (clock→id, run before every windowed read incl. the admin Activity page) no longer
scans the whole store. `MIN(id) WHERE recv_ts >= ?` could NOT use the recv_ts index — SQLite walked the
rowid from id=1 to the first match, so a recent "from" bound scanned almost every frame and grew with
the store (~285 ms at 1.5M frames). Rewritten as an indexed `ORDER BY recv_ts … LIMIT 1` seek
(idx_frames_recv_ts), O(log n) (~0.06 ms). Exactly equivalent because recv_ts is non-decreasing with id
(frames added in receive order), so the first frame by recv_ts is the min id in range.

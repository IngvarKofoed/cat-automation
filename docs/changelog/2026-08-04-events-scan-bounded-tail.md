# Activity feed scans only the newest 200k motion frames

<!-- entry 102 -->

Activity feed (`events()`) no longer slows as the store grows: it read EVERY motion frame in history
and temp-b-tree-sorted them on every call (twice per page load — the feed AND `cats_overview`), so the
scan cost climbed with the store and inflated further under lock contention with the collector/live
worker (the 13 s `/api/events`, 49 s `/api/cats/overview`). Now it scans only the newest
`_EVENT_SCAN_FRAMES` (200k) motion frames — the feed's newest events live in the recv_ts tail — via a
DESC+LIMIT read served in-order by new index `idx_frames_motion_recv (motion, recv_ts)`, no sort,
bounded regardless of store size. A scan-capped window drops its oldest (possibly-partial) cluster and
sets `truncated`. Existing stores build the index on next open.

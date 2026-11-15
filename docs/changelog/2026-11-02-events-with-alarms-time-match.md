# Episodes attach to the one visit holding the deciding frame by time

<!-- entry 550 -->

`Store.events(with_alarms=True)` — `/api/events` only — gives each episode to the ONE visit
whose `[start_ts, end_ts + _VISIT_GAP_MS]` holds its deciding frame's recv_ts. Unique because
`_gap_split` splits on a gap strictly over the threshold. Matched by TIME, never frame_id
(reused after a Clear). An episode whose visit is off the feed shows only in admin's log —
it fails toward no mark, never toward a mark on the wrong visit.

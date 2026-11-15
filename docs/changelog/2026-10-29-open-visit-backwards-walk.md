# open_visit walks backwards and alarm_frames pins join order

<!-- entry 529 -->

`open_visit` walks BACKWARDS from the newest motion frame, costing the open cluster, not
the backlog: the watermark lags hours while a manual job holds the closed pass off.
`alarm_frames` pins `frames` as the outer loop (`CROSS JOIN`) — unpinned, with no ANALYZE
stats, it walks the oracle's whole partition: entries 229/265/276/307/385/391, seventh
instance. Both plans are pinned by tests; measured ~0.1 ms each at 300k frames.

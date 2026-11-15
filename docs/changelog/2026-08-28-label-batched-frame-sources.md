# POST /api/label resolves frames in one batched read

<!-- entry 220 -->

`POST /api/label` resolves a visit's frames in ONE batched read (`Store.frame_sources`)
instead of a `frame_recv_ts` + `path_for` pair each: those take the shared write lock, so a
40-frame visit took it 80 times, every acquisition queueing behind the collector's inserts
(the contention class entries 102-105 removed). It also closes a small race — the old pair
could see a frame evicted between the two reads.

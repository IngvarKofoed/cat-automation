# events() reads annotations per event span

<!-- entry 256 -->

`events()` reads its annotations PER EVENT SPAN, not over the events' overall
[min start_id, max end_id] envelope. That envelope stretches across every frame between
the newest and oldest event, so under continuous capture ~95% of the rows it fetched
belonged to no event — materialised, then discarded, all under the shared write lock.
Measured at 1.5M frames with a full YOLO sweep, on a store with NO `ANALYZE` stats
(what the real one is): `Store.events` 1415 ms → 54 ms for the same 500 events.

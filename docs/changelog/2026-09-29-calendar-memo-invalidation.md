# Every mutation that can move a day drops its memo

<!-- entry 382 -->

Nothing about that is a heuristic: every mutation that can move a day drops it.
Frame deletes and every `analysis` write/clear match by id span
(`_calendar_invalidate_locked`); a frame INSERT — whose id is above every existing
span — matches by timestamp instead. The memo and its invalidation both live in the
DB, so a second process's write is seen by the running server (verified).

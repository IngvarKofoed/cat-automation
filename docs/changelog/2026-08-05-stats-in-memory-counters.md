# stats() keeps frame counts in memory, O(1)

<!-- entry 104 -->

`stats()` no longer scans the whole store. It read `COUNT(*), SUM(motion) FROM frames` — an O(store)
full scan — and the dashboard polls it every ~4 s, so that recurring scan held the shared lock and
starved the collector and tuning sweeps as the store grew. Frame/motion counts are now kept in memory
(`_count`/`_motion_count`), seeded once at open and maintained in lockstep by add/evict/clear exactly
like `_total_bytes`; the recv_ts span is two O(1) index-endpoint seeks. stats() is now O(1), no scan.

# Preferential eviction orders victims by recv_ts, not id

<!-- entry 309 -->

Preferential eviction orders victims by `recv_ts`, not `id`. `WHERE motion = 0 ORDER BY id`
has NO index serving it, so SQLite temp-b-tree-sorted the whole remaining non-motion
partition to return 64 rows — once per `add` (~10/s) under the write lock, for the whole
multi-day shedding. Measured on the real schema with no ANALYZE: 7.5 ms at 238k non-motion
rows → 34 ms at 950k → 63 ms at 1.9M, i.e. linear, vs a flat 0.02 ms ordered by `recv_ts`
off the existing `idx_frames_motion_recv`. At cap that was ~0.8-2.6 s of lock hold per
second of capture: total starvation, entries 102-105 again.

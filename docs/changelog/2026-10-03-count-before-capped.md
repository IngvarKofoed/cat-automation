# count_before_capped replaces the exact pre-window count

<!-- entry 401 -->

`count_before_capped` replaces the Compare's exact pre-window frame count, which was a
rowid scan from id 1 to the window (66 ms at 6M frames, growing) for a number that
saturates at 500. It matters beyond the milliseconds: that scan held the shared write
lock against the collector on every click — the starvation class of entries 102-105.
Named apart from `count_in_range` because it returns `min(preceding, cap)`, not a total.

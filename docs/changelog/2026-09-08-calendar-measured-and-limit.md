# Calendar measured speedup and remaining O(store) cost

<!-- entry 277 -->

Measured on a 2.5M-frame / 4.3M-verdict replica of the real store: that pass 2590 →
880 ms, the whole call 3.0 → 1.4 s (the live endpoint was 4.8 s). Known limit — it is
still O(store), walking every frame's recv_ts index entry (390 ms) plus every verdict
in the window on EVERY open, and the store sits at 30% of its 1 TiB cap. Structural
fixes are maintained per-day counters or a cache; the always-on oracle writes verdicts
continuously, so a whole-store epoch key would never hit.

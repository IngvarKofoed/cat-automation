# Stats accumulate in float64 without copying float32 pair arrays

<!-- entry 464 -->

Review repairs to the above. `_stats`' `np.asarray(a, dtype=float)` was a no-op while
the pair arrays were float64 and became a full COPY the moment they went float32 —
~2.4 GB for the `diff` side at 27k crops, allocated while everything else is still
live, i.e. the float32 change partly undoing itself. It now accumulates in float64
(`mean(dtype=)`) without touching the array: n=7998 peak 1541 -> 1305 MB.

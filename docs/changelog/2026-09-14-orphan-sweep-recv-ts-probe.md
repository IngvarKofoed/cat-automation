# Orphan sweep probes referenced rows through the recv_ts index

<!-- entry 307 -->

The orphan sweep's referenced-row probe now goes through the `recv_ts` INDEX, not a bare
`WHERE path = ?` — `frames.path` is unindexed, so that was a FULL TABLE SCAN per candidate
file: O(files x rows), quadratic in store size. Measured at 20k files / 20k rows:
11 s → 0.46 s, now walk-bound (≈1 min extrapolated to 2.5M files, vs effectively
unbounded). `add` composes the name as `<date>/<hour>/<recv_ts_ms>_f<id>.jpg`, so the
millisecond is recoverable; the `path` equality still runs, so the answer is identical and
only the plan changed. Entries 229/265/276, fourth instance.

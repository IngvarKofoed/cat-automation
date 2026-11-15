# Tuning calendar counts verdicts per day span, no join

<!-- entry 276 -->

The Motion-tuning calendar ("Last 4 weeks") no longer joins `frames` per verdict, and
carries `+analyzer` — entries 229/265's mis-plan, third instance: with no ANALYZE stats
SQLite scanned each analyzer's WHOLE partition and fetched every row. Each day's
verdicts are now COUNTed over that day's own id span off the `(frame_id, analyzer)` PK,
a pure covering scan — equivalent because recv_ts is non-decreasing with id and
`analysis` cascade-deletes with its frame.

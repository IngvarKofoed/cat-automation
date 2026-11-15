# Visit stats count the events the feed produces

<!-- entry 341 -->

It counts the events `Store.events` produces rather than querying `identifications`,
as `cats_overview` does and for the same reason: every visit is clustered by
`_gap_split` and named by `_aggregate_identity`, so Visits, Cats and Activity can
never name a moment differently — and the uncalibrated fail-safe (a NULL threshold
names nobody) carries over for free.

# labeled_visits moved to its own WAL read connection

<!-- entry 197 -->

`Store.labeled_visits` moved to its own short-lived WAL read connection, and returns per-frame
`score` + per-visit `peak_area`/`peak_score` (the re-seed inputs above). Unbounded, it walks
every positive oracle verdict probing `dataset_items` per row — holding the shared write lock
for that is the collector starvation entries 102-105 removed, and the live admin now calls it.

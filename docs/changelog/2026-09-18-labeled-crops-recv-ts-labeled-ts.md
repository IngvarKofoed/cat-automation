# labeled_crops returns src_recv_ts and labeled_ts from dataset_items

<!-- entry 323 -->

`labeled_crops` now returns `src_recv_ts` + `labeled_ts`. Both come off
`dataset_items`, NOT `frames`, so grouping and day/night bucketing cover every label
including those whose frames have aged out. `labeled_ts` is stamped once per commit, so
distinct values ≈ label keypresses — a free cross-check on the re-derived grouping,
printed in the report beside it.

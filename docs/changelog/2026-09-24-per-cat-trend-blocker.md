# Per-cat trend blocked: confusion matrix rows carry no cat identity

<!-- entry 357 -->

Known blocker for a per-cat trend, found while scoping one: the visit confusion matrix
IS persisted, but nothing records WHICH CAT each row is. `_run_metrics` keeps only
`visits` + `excluded_cat_ids`, dropping the top-level `cats` index→id map, and the index
is positional over the cats present in that run — so excluding one cat shifts every
index above it and two runs' row 3 are different cats. Existing rows are untrendable
and cannot be back-filled (a re-run measures today's labels). Fix is additive: persist
`cats` beside `visits`; the trend then starts from the next run.

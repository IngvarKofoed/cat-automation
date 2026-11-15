# crop_rel_path lives in dataset.crops so commits and re-cuts agree on paths

<!-- entry 468 -->

`crop_rel_path` moved from `compute.tools.recut_crops` into `compute.dataset.crops`,
which both the commit path and the tool depend on — the API importing a CLI tool was
backwards. Load-bearing, not tidying: a fresh crop must land on exactly the path a
re-cut to that shape would write, or the re-cut cannot RELINK it and re-cuts from the
frame instead, which fails outright once that frame has evicted.

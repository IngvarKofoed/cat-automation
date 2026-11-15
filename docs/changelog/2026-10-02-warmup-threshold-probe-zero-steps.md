# Warm-up threshold probe costs zero VDBE steps

<!-- entry 393 -->

The warm-up-threshold probe now costs ZERO VDBE steps. `ORDER BY f.id` is free in
rowid order, so `LIMIT 1` stops at the first scored row instead of materializing the
window into a temp b-tree — the pin's largest single win, and invisible in the SQL.

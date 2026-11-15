# Per-span reads carry +analyzer to avoid a 36x regression

<!-- entry 265 -->

The per-span reads carry `+analyzer`, DE-INDEXING that term as `_resolve_flag` does.
Without it entry 256 was a 36x REGRESSION, not a fix: the store runs no `ANALYZE`, so
with no `sqlite_stat1` SQLite prefers `idx_analysis_analyzer_verdict` on the equality
and scans the whole yolo-serial partition — once PER SPAN. One 100-event page at 1.5M
frames: 19.5 s unhinted vs 4.8 ms hinted (the old envelope query was 538 ms).
Entry 229 predicted exactly this and named `events()` as still carrying it.

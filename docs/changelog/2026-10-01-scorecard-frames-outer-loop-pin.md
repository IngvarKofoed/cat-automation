# Scoped gate scorecard pins frames as the join's outer loop

<!-- entry 391 -->

A SCOPED gate scorecard pins `frames` as the join's outer loop (`CROSS JOIN`, which
constrains order in SQLite without changing meaning), so a one-day Compare costs the
day instead of the store. Entries 229/265/276/307/385, fifth instance and the worst:
with no ANALYZE stats SQLite drove from `idx_analysis_analyzer_verdict`, walked the
oracle's WHOLE partition and applied the window bound only after probing `frames` —
three queries deep, three columns wide.

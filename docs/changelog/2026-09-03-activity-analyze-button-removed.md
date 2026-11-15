# Admin Activity lost its Analyze button

<!-- entry 249 -->

Admin Activity lost its Analyze button (entries 90/91) — removed as unused. Re-detecting a
historical window is now only a Motion-tuning sweep, which is also where its progress always
showed; Identify stays as Activity's one backfill. `/api/analysis/run`'s `reanalyze` +
`motion_only` are untouched (the tuning sweeps use both), so this dropped a caller, not a path.

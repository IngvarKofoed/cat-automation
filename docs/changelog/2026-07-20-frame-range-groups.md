# Named frame-range groups scope sweeps, re-runs and scorecards

<!-- entry 26 -->

Frame-range groups: named, contiguous [start_id, end_id] windows scope oracle sweeps, MOG2 reruns, and
scorecards to time slices. Bounds (since_id/until_id) thread through Store reads and API endpoints.
Scoped reruns warm-start from the frames just before the window and clear only that window's verdicts;
scoped scorecards drop only the still-unprimed prefix (0 when fully primed, full warm-up at the store's start).
Persist via /api/groups CRUD (new groups table); groups survive eviction but drop on full clear() (rowid reuse).

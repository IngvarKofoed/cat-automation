# Select all button makes the pending bucket the loaded window

<!-- entry 38 -->

Buckets "Select all" button: one click makes the pending bucket the whole loaded window
(its resolved [since_id, until_id]), instead of hunting for the exact first/last tile in a
decimated grid. Uses the resolved id bounds, not on-screen tiles, so density/paging can't
truncate it. GET /api/frames/resolve now also returns since_ts/until_ts so the readout labels
the selection with real frame times.

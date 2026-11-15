# cleanup/run takes a recut kind with a separate plan endpoint

<!-- entry 451 -->

`POST /api/cleanup/run` takes a third kind, `recut`, with `GET /api/recut/plan` as its
separate reader (a census plus a per-branch breakdown is nothing like the estimate
endpoint's `{count, bytes}`). `geometry` is REQUIRED there — absent means legacy for a
READ and would mean an unrequested whole-set move here — so legacy gets the CLI's own
`legacy` sentinel, or it becomes the one target reachable onto but never back from.
Absent on the PLAN endpoint still means census-only.

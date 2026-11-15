# A day is reserved pending and filled by a token-guarded update

<!-- entry 383 -->

A day is reserved PENDING before the slow passes and filled in by a token-guarded
UPDATE after, so a mutation landing mid-compute deletes the placeholder and the
fill-in matches nothing. Without it the naive "just write the result" form caches
numbers already stale — proven by running its test against that form.
A pending row is never read, so a crash mid-compute leaves no result behind.

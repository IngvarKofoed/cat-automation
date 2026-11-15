# Memo pruning reads before it writes and reaches both ways

<!-- entry 389 -->

Pruning the memo now reads before it writes, and reaches BOTH ways. A fully memoized
call previously opened a write transaction and committed on the shared connection to
delete nothing — the exact traffic this feature moved off it — and days ABOVE the
window accumulated forever, since only days below were pruned.

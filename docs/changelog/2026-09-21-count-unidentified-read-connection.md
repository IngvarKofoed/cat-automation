# count_unidentified moved to its own WAL read connection

<!-- entry 338 -->

`count_unidentified` moved to its OWN short-lived WAL read connection. UNBOUNDED —
the identify pass's progress denominator — it measured 4.9 s on a 4.7M-frame replica
with no `ANALYZE`, stalling a competing writer 7.2 s on the shared connection vs
0.1 ms off it (measured). The starvation class entries 102-105 removed; latent until
entry 337's button made the unbounded call routine.

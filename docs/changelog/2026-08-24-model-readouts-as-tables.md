# Model building readouts are tables

<!-- entry 200 -->

Model building's three readouts are now tables in the sweep-queue style, not one-liners.
The running/queued TRAINING jobs render through the shared `renderQueue` — build,
validation and identify together, since they share one FIFO — with a per-run rate + ETA;
`renderQueue` gained optional unit-bearing headers ("Items · Rate", the manager counts
crops not frames) and a `cancelable:false` row. A pending training job carries no job id,
so only the running row offers Cancel and one "Clear queue" drops the pending — a per-row
button there would have lied about what it does.
Validation runs and gallery versions became 8-column tables of the same geometry.

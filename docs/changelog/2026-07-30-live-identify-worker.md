# Live worker names new closed visits automatically

<!-- entry 74 -->

Activity page now names new visits automatically: `LiveIdentifyManager` (mirroring `CollectorManager`)
ticks every 5s over closed motion clusters (settled ≥ `_VISIT_GAP_MS`), running `yolo-serial` detect +
`run_identify` against the active gallery per cluster. Reads `active_model()` each tick (promotion live),
yields GPU to manual jobs, holds resident detector+embedder. `run_identify` accepts optional `embedder`
to avoid per-tick reload. Historical re-identification needs manual pass; worker runs only on compute PC.

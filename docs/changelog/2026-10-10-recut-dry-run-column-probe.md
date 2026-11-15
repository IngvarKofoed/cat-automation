# recut_crops probes for the geometry column before reading it

<!-- entry 437 -->

`recut_crops` probes for `dataset_items.geometry` before reading it. The column is added
by `Store._migrate_schema`, which runs only in `Store.__init__` — and the tool builds a
Store only on `--apply`, so its DRY RUN (the first command its own docstring gives) died
with a bare `no such column` on any store this build had not yet opened. Probe, not
migrate: a dry run keeps its write-nothing contract, and the answer is an instruction.

# feasibility_runs gained a metrics column via first additive migration

<!-- entry 322 -->

`feasibility_runs` gained a `metrics` JSON column (mirroring `model_versions.metrics`),
carrying the visit block so the runs table can rank by the honest number. This is the
repo's FIRST additive migration: the schema is `CREATE TABLE IF NOT EXISTS` only, which
never adds a column to an existing table, so `_migrate_schema` probes `PRAGMA
table_info` and `ALTER TABLE ADD COLUMN`s. Pre-existing rows read NULL = "not measured",
which the runs table renders as `—`, distinct from an uncalibrated run's `n/a`.

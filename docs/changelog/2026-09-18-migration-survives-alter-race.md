# Schema migration tolerates losing a cross-process ALTER race

<!-- entry 326 -->

The schema migration survives losing a cross-process race. The `PRAGMA table_info`
probe and the `ALTER TABLE` are not atomic across processes — the store lock is
per-instance and `busy_timeout` only bounds waiting for the write lock — and the API
plus the CLI tools legitimately open the same `index.db`. The loser now no-ops on
`duplicate column` instead of dying inside `Store.__init__` with what reads like
schema corruption. Narrow on purpose: "database is locked" and disk-full stay loud.

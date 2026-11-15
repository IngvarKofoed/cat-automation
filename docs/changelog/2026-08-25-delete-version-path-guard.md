# Version delete verifies gallery dir is under models root

<!-- entry 206 -->

`delete_model_version` verifies the resolved gallery dir sits under `models_root`
before the rmtree. `gallery_dir` is a bare basename written by our own builder, but
this is the one call that recursively deletes a directory NAMED BY A DB COLUMN — a
hand-edited row must not turn it into "remove any path". Row + identifications are one
locked txn; the dir goes after the commit, outside the lock (filesystem work must not
hold the shared write lock), so a failed removal leaves an inert orphan dir rather
than a row promising a file that is not there.

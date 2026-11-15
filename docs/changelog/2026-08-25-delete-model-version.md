# A gallery version can be deleted

<!-- entry 205 -->

A gallery version can now be DELETED — `Store.delete_model_version` + `DELETE
/api/training/models/{id}`, a Delete button on every non-active row. `model_versions`
survives eviction and `clear` by design, so it accumulated a row plus a `gallery.npz`
per build with no way to clear either. The version's `identifications` go too: every
read is scoped to a `model_version_id` and the id is AUTOINCREMENT (never reused), so
they are unreachable once the row is gone.
The ACTIVE version is refused (409) — identification reads it every tick; promote
another first. Irreversible: no rollback to it, and the visits it named lose their
names. Labels and crops are never touched, so a rebuild restores it.

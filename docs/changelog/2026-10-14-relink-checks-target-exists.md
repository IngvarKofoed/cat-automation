# relink re-checks the target file exists at write time

<!-- entry 455 -->

Review repairs. `relink` now re-checks the target file exists at WRITE time. `_route`
decided it at read time and the branch does no I/O, so unlike copy/recut nothing else
would notice it had gone — a manual reclaim or a concurrent relabel's variant-delete
landing mid-run would stamp the row onto a missing file and report it MOVED, the one
outcome the module promises cannot happen. Narrows the window rather than closing it.

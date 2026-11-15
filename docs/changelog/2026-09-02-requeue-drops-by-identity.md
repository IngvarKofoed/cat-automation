# Requeue drops the deleted visit by identity

<!-- entry 245 -->

`requeue` drops the visit it DELETED, located by identity, not whatever `labIdx` points at
after the await. Navigation was never busy-guarded, and the overview's clickable tiles made
the race easy: navigating mid-delete spliced a different, still-labelled visit out of the
list while the deleted one stayed on screen looking fine. `labAll` and `dropFlagged` already
located by identity — Labelled's `lab` was the one place left trusting a live index.

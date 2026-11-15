# Lighting flag POST requires threshold and is hardened

<!-- entry 182 -->

Review hardening of the lighting flag. `POST /api/lighting` now REQUIRES `threshold`
(nullable) and rejects booleans, so an empty body can't silently wipe a calibrated
cutoff and `true` can't coerce to a 1.0 one that reclassifies the whole store as night.
`lighting_histogram` + the new `lighting_staleness` run on their OWN short-lived WAL
read connections — the operator hits them on every day click, and two unbounded
`analysis` aggregates under the shared write lock is the starvation class 102-105 removed.
`gate_scorecard`'s `interesting` present flag is now THREE-valued, matching SQL: a
positive verdict with a NULL score under an `oracle_floor` is UNKNOWN and counts on
neither side, so the Python recount can no longer invent false triggers and break the
day + night == All partition. A scoped card also drops the unscoped `split` key.
Staleness is matched with `json_extract`, not a LIKE — `"version": 1` is a substring
of `"version": 10`.

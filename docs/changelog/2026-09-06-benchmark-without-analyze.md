# Measure query plans on a store with no ANALYZE

<!-- entry 266 -->

Measure query-plan work on a store with NO `ANALYZE` stats. A bench that runs `ANALYZE`
gets a plan the real store never gets, which is how entry 256's numbers came out both
too optimistic and measured against the wrong plan — the mis-plan above was invisible
until the same queries were re-timed without stats.

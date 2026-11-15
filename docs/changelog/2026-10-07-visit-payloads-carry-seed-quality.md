# Every visit payload asserts it carries seed_quality

<!-- entry 419 -->

Every visit payload asserts it carries `seed_quality`, not just the queue's. The client
echoes that field straight into `POST /api/label`, where `quality` defaults to None, so
a builder that stopped attaching it would write NULL-grade crops a quality-filtered
build then skips — silently. These assertions replace the `canSeed` guard that used to
catch that state and was deleted with the client formula (entry 416).

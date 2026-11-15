# Daily count cached 10 minutes, single-flight, keyed on model

<!-- entry 558 -->

That count is kept in memory 10 minutes, single-flight, keyed on (local date, active model id,
threshold) — taken from what the walk ACTUALLY read, so a promotion mid-walk can't file it
under the old model. The `split` is cached WITH the counts (a location set inside the ten
minutes must not pair `available: true` with counts made without it). Rejected: a persisted
per-day memo — every rename path would need the exact invalidation `calendar_days` took.

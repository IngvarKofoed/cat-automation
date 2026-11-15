# Watermark seeding has a single epoch-bumping write path

<!-- entry 150 -->

`_seed_watermark` is now the single write path for a watermark that did NOT come from a completed
sweep (`start`'s horizon seed + `/api/clear`'s `reset_watermark`), so both carry the epoch bump that
stops an in-flight tick writing its stale derived value back. `_seed_horizon` is gone from the oracle
— with every start seeding, there is no first-enable-only flag left to accidentally consume.
`live_identify` keeps its own flag: it back-identifies visit spans, a different contract.

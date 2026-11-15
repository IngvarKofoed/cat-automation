# Previous-alarm floor is seeded from the log at startup

<!-- entry 584 -->

The previous-alarm floor is SEEDED from the newest `mode='on'` row's `end_ts` when the
worker is built: a 2-minute lookback would otherwise let a restart recount the last episode's
stranger. Load-bearing order: the launch sweep that closes open rows now runs BEFORE the
worker is constructed — an open newest row seeds no floor.

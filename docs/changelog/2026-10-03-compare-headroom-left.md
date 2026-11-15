# Compare headroom deliberately left: per-card memoization

<!-- entry 402 -->

Remaining Compare headroom, deliberately left. Each column still walks its window
twice (aggregate + `interesting`), ~2 s apiece on the replica; fusing them saves only
~20% since the walk, not the arms, is the cost. The structural fix is memoizing a card
per (day, source, oracle, params, floor) as `calendar_days` does for the calendar
(entry 381) — a feature with real invalidation subtleties, not a patch.

# Calendar assumes recv_ts is monotonic with id

<!-- entry 278 -->

What entry 276 trades away: `recv_ts` monotonic with `id` is ASSUMED, not enforced —
the collector stamps the wall clock — so a backward step across a local midnight
overlaps two days' id spans and the earlier day counts the later one's verdicts.
It fails LOUDLY, one direction only: coverage reads above 100%, never a silent
under-report. Left unguarded because clamping to the next day's first id regresses
the OPPOSITE skew into a fully-swept day reading 0%; only the join is immune.

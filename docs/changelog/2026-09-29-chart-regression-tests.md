# Both report charts gained regression tests

<!-- entry 380 -->

Both charts gained tests — a regression to "" would otherwise drop a figure from every
report silently. They pin the empty cases each returns nothing for (no rows, nothing
decided, no regimes, a cat present on only one side, mismatched cat_ids across
regimes) and that the tile and the chart cannot disagree on a tie, verified to FAIL
against the pre-fix rule rather than merely passing after it.

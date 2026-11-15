# _score_visits returns per_cat rows keyed by cat_id

<!-- entry 359 -->

`_score_visits` returns `per_cat`, derived beside the `accuracy` built from the same
counts so the two can never contradict each other. Because `_visits_block` spreads
that return and each `regimes[name]` IS a `_score_visits` return, one addition lands
per-cat rows on the mixed block AND both regimes — and the whole `visits` block was
already persisted and lifted, so `runner.py`, `probe.py` and the store are untouched.
Rows self-identify by `cat_id`, retiring the positional confusion index (entry 357)
rather than teaching a second consumer to read it.

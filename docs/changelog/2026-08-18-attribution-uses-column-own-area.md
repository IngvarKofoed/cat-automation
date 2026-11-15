# Miss attribution uses each column's own area

<!-- entry 171 -->

That attribution uses each column's OWN area — `f.area` for Live, the slot's `analysis.score` for
a re-run — so Live and Candidate can name different knobs for the same visit. Buckets are
per-visit and EXCLUSIVE (they sum to `n_present`), unlike `gate_scorecard`'s window-wide
`area_buckets` where `near_zero` is a subset of `below_min` and one long visit dominates the
frame-weighted totals. New `classify_miss` mirrors `MotionGate.process`'s band test.

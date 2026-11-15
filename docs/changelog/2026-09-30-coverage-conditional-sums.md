# Coverage pass counts with conditional SUMs, retiring the analyzer hint

<!-- entry 385 -->

The coverage pass counts with conditional SUMs, retiring the `+analyzer` hint
(entries 229/265/276/307) rather than depending on it: with no analyzer predicate
at all there is nothing left for the planner to mis-choose. Measured 10.1 -> 8.0 s
over 16.2M verdicts — the hint had also forced a temp b-tree for its GROUP BY.
Dropping the hint from the OLD form instead was 45x worse, so it was load-bearing.

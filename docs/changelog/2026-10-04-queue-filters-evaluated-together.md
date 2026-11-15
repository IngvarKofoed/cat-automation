# Queue filters are evaluated together, not sequentially

<!-- entry 407 -->

The two filters are evaluated TOGETHER, replacing the sequential `uncertain_only`
block, because sequencing makes each count depend on which ran first. Both orderings
produce the SAME surviving set and differ only in the counts, so nothing catches a
re-sequencing by inspection — pinned by a test asserting each untick reveals its quoted
number, verified to FAIL against the sequential form (`hidden_confident` 3 vs 2).

# Probe is still O(n^2); fit at 27k crops depends on compute RAM

<!-- entry 463 -->

Known limit, and the reason this is a reprieve rather than a fix: the design is still
O(n²), so the same fit puts ~27k crops at ~13 GB (against ~57 GB before). Whether a
run fits is now a question about the compute PC's RAM. The next lever is subsampling
the `diff` side — 365M samples estimates a distribution 10M would — but that moves
the numbers, so it is not a silent change.

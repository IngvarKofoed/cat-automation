# Feasibility probe runs in float32 and avoids n-squared index arrays

<!-- entry 459 -->

The feasibility probe runs in FLOAT32 and no longer materialises what it can index
around. A real run at ~27k crops died allocating 2.72 GiB inside `_best_threshold`;
the labelled set had simply outgrown an O(n²) design nobody had re-measured since
entry 329 sized it at n=6000. Four changes, none touching a reported number:
float32 throughout, no similarity/`d_knn` twin matrices, blockwise kNN argsort, and
row-wise upper-triangle construction in place of `np.triu_indices`.

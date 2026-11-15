# Threshold candidates are same-cat distances only, which is exact

<!-- entry 461 -->

`_best_threshold`'s candidates are the SAME distances only, never `same ∪ diff`, and
that is exact rather than a sampling: between consecutive same-values TPR is flat
while TNR is non-increasing, so the optimum always sits at a same-value, and the
argmax tie-break agrees because the dominating candidate is the smaller one. `diff`
is ~95% of pairs here, so this alone is what the fatal 2.72 GiB allocation was.

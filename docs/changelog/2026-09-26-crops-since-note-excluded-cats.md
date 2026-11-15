# Crops-since note sums over the same cats the run used

<!-- entry 367 -->

Review repairs to the above. The "+N crops labelled since" note counted EXCLUDED cats
on one side only — `enrollable_cats` returns every active cat while the run's
`n_crops` was counted post-exclusion — so holding a cat back made the stamp report its
whole crop count as newly labelled, on every render, forever. Now summed over the same
set the run used, which `runComparable` has already established.

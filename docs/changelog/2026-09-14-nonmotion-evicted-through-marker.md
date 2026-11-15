# Marker records partially stripped windows

<!-- entry 304 -->

That preference strips windows PARTIALLY (motion frames present, non-motion gone), which
plain oldest-first never did — so it advances a `nonmotion_evicted_through` marker that
`motion_only_spans` folds in as the prefix `[1, N]`. Without it a scorecard reads
near-perfect gate recall over frames that were deleted: entries 97/126/167's trap, in the
one place it would be least visible.

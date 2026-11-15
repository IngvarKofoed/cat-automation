# Opt-out of dataset_items reads pinned for overview and stats

<!-- entry 520 -->

The opt-out invariant is now pinned by a test: `cats_overview` and `door_stats` must
issue no `dataset_items` read, since paying for a key they never look at is what
`with_labels` exists to prevent — and `door_stats` pages this feed 8x. Verified to fail
when `cats_overview` is flipped to opt in.

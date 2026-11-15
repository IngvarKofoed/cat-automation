# Held-out visits grouped per cat at a coarse 60 s gap

<!-- entry 319 -->

Held-out visits are grouped PER CAT, at a coarse 60 s gap of their own
(`_HELDOUT_GAP_MS`), not the store's 2 s `_VISIT_GAP_MS`. Coarse is fail-safe here:
over-merging removes more leakage, under-merging splits one physical visit across the
boundary and lets the near-duplicates back in. A global time-sort would also merge two
cats at the door in one minute into a group with no true `cat_id` — tailgating is
expected here, and `dataset_items`' UNIQUE only stops two cats sharing ONE frame.

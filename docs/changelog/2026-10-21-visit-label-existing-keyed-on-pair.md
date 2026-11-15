# Existing-label lookup keys on frame id and recv_ts pair

<!-- entry 490 -->

`visit_label_state`'s `existing` keys on the (`src_frame_id`, `src_recv_ts`) PAIR
like every sibling reader, not on the id alone. `frames.id` has no AUTOINCREMENT and
`clear()` deliberately spares `dataset_items`, so a new visit on a reused id range
matched a stale pre-clear row and the phone read "Labelled: OldCat" over a visit
nobody had labelled — in the feature whose purpose is finding mislabels. A row whose
frame merely EVICTED is still kept: eviction never reuses an id.

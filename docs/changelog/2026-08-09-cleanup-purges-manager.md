# Cleanup runs cancelable non-motion purge and orphan sweep

<!-- entry 126 -->

Cleanup purges (admin-next P7): `CleanupManager` runs two batched, cancelable background jobs that
release the store lock between batches — drop non-motion frames (through the eviction accounting path,
never a raw DELETE; a mid-batch error rolls back and resyncs counters so `_count`/`_motion_count`/
`_total_bytes` can't drift) and sweep orphaned JPEGs (frames media dir only). Never touches
`dataset_items`/crops/`model_versions`. A non-motion purge records a `purge_spans` marker so later
scorecards/coverage over that window warn "misses unmeasurable". `/api/cleanup/*`; the reclaim estimate
counts exactly and approximates bytes (avoids an O(store) SUM scan under the lock).

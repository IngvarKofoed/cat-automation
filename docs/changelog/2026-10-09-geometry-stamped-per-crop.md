# Geometry is stamped per crop and a build selects one

<!-- entry 429 -->

Per-crop, not global, because the second geometry change after any frame eviction would
otherwise leave two conventions side by side with no record of which is which — and
1-NN over a blend of two feature spaces is silently worse matching with no symptom.
`build_gallery` therefore SELECTS one convention rather than filtering to "any", and
`compute.tools.recut_crops` re-cuts what it can (only rows whose source frame is live)
through the new `Store.update_dataset_geometry`.

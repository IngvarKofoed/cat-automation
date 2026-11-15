# Galleries are versioned and promoted one-active-at-a-time

<!-- entry 67 -->

Gallery-build + promote: new `model_versions` table (survives eviction/clear like `cats`/`dataset_items`)
versioning k-NN galleries built from labelled crops. `gallery-build` is a TrainingManager job embedding
selected-quality `identified` crops and writing their vectors+cat_ids to `<CAT_COLLECT_DIR>/models/<ts>/gallery.npz`.
`promote` is synchronous (flips target→active, current-active→retired, one active at a time — rollback by promoting retired).
Spec: docs/specs/2026-07-17-identification-gallery-activity.md.

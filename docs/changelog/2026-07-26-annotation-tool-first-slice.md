# Per-visit keyboard annotation writes durable labelled crops

<!-- entry 57 -->

Cat-identity annotation tool (compute `#annotate`) — first slice of the learning loop: per-visit keyboard
labelling of live `yolo-serial` detections (clustered via `_gap_split`) into per-frame `dataset_items` rows +
durable crops under `<CAT_COLLECT_DIR>/dataset/`, each tagged quality `gallery`/`ok`/`poor` for a future gallery.
New `cats` + `dataset_items` tables SURVIVE eviction AND `clear()` — labels are the precious output, decoupled
from the rolling frame buffer; dedup on `(src_frame_id, src_recv_ts)` defeats a `clear()`+rowid-reuse mislabel.
Deferred: training/gallery-build, in-tool undo/re-label, and `annotation_visits` pagination.
Spec: docs/specs/2026-07-15-annotation-tool.md.

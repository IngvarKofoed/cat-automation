# Milestone M3 — The learning loop's first build

<!-- milestone M3: entries 57–79 -->

**M3 — The learning loop's first build** (57–79). Per-visit keyboard annotation writes durable crops
plus `dataset_items`; `cats`, `dataset_items` and `model_versions` survive eviction *and* `clear()` —
labels are the precious output. A DINOv2 probe scores separability; gallery build → promote keeps
exactly one active version, and the identify pass stores per-frame nearest-neighbour distance with the
threshold applied at **read** time so it stays tunable. An uncalibrated (NULL-threshold) gallery names
nobody, and a live worker names new visits without back-identifying history.

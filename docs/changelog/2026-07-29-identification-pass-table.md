# Identify pass stores per-frame nearest-gallery matches

<!-- entry 68 -->

Identification pass: new frame-keyed `identifications` table (evicts with frames, like `analysis`)
storing per-frame nearest-neighbour match to active gallery. `identify` TrainingManager job embeds
yolo-serial-detected crops from live frames, matches to gallery, stores cat_id+distance (no threshold baked).
Threshold lives on model_versions row, applied only at read—always tunable without re-identify.
Resumable, idempotent per model.

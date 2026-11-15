# Crops carry a geometry stamp, with letterbox over squash

<!-- entry 428 -->

Crops carry a GEOMETRY: `letterbox` (aspect-preserving pad, at the normalisation-zero
mean — black would inject a constant into every vector) and an optional context margin,
replacing the anisotropic 224×224 squash that distorted boxes up to 4.8× and differently
per frame within one visit. `dataset_items.geometry` stamps it PER CROP; NULL = legacy,
which every crop cut before this is. Default stays legacy so promoted galleries keep
matching their own queries.

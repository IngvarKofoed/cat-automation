# Feasibility probe scores cat separability with DINOv2

<!-- entry 59 -->

Feasibility probe (`compute/identification/` + `compute/tools/feasibility.py`) answers Phase-1's "can we
tell our cats apart?" over labelled crops — offline, read-only, NO training. Embeds `identified` crops
(new `Store.labeled_crops`) with a pretrained DINOv2 backbone (`torch.hub`, lazy/torch-gated like the YOLO
oracle — first run downloads it), then scores separability: leave-one-out kNN accuracy + confusion,
same-vs-different-cat cosine-distance AUC + a suggested confidence threshold, and a PCA-2D scatter — emitted
as a self-contained HTML report + JSON. Separability maths is pure-numpy (unit-tested with synthetic
vectors); DINOv2 + matplotlib are opt-in analysis extras. Runs on the compute PC (labels + net + GPU there).

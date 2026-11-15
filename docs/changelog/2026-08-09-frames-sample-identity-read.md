# Frames sample can attach per-frame identity matches

<!-- entry 124 -->

Identity read API (admin-next P3): `GET /api/frames/sample?identify=1` attaches each frame's
nearest-gallery match {cat_id, name, is_resident, distance, resolved} for Frame-review overlays,
reusing the active model's read-time threshold and uncalibrated fail-safe (an uncalibrated model
resolves every frame to "unknown"). Additive — byte-identical without the flag.

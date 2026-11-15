# Edge applies a persisted rotate and crop to every frame

<!-- entry 8 -->

Edge applies per-frame rotate+crop transform: rotation (0/90/180/270) and normalized clip persist in settings.json.
`/frame` returns rotated+cropped door region; `/frame?raw=1` returns rotated+uncropped for ROI editing.
`POST /api/config` accepts any field subset (device now optional) and persists full config
before swapping source, so bad values fail safe to defaults. Foundation for motion gate and `/stream`.

# Tuning compare reports the params behind each column

<!-- entry 158 -->

`GET /api/tuning/compare` now reports the params BEHIND each column (`params.{live,baseline,candidate}`),
so a UI shows what a scorecard was produced by instead of whatever is currently typed in the fields —
those diverge the moment the operator edits after a re-run. All three are normalised to the EDGE
vocabulary: a slot stores `MotionParams.downscale`, and without the rename a key-for-key diff would
silently drop `motion_downscale`. A slot that never ran reports null, not the edge config as if it were its own.

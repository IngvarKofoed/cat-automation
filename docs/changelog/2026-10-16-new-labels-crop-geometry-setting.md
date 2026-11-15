# New labels are cut and stamped at a persisted crop_geometry setting

<!-- entry 467 -->

New labels are cut and stamped at a persisted `crop_geometry` setting
(`GET`/`POST /api/crop-geometry`; the editable control is Tools → Crop shape).
`_commit_label` cut with no margin and stamped nothing, so every label landed LEGACY
no matter what the labelled set held — a store re-cut to one shape re-split the moment
anyone labelled again, entry 441's open gap. Unset still means legacy, so an untouched
install is byte-identical. Spec: docs/specs/2026-08-11-crop-geometry-for-new-labels.md.

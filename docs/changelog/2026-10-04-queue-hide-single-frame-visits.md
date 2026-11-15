# Annotation queue hides single-frame visits by default

<!-- entry 403 -->

The annotation queue gained "Hide single-frame visits" (`min_frames`), ON by default —
unlike the confidence filter beside it, because thin visits are the everyday case. A
one-frame visit yields ONE crop, which will not be gallery-grade, so a gallery-only
build AND a gallery-only validation run both ignore it entirely: labelling it costs
attention and returns nothing. Spec: docs/specs/2026-08-09-annotation-queue-min-frames.md.

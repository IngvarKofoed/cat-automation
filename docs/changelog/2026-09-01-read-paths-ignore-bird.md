# Read paths ignore legacy bird boxes

<!-- entry 239 -->

Every read path that ACTS on a class dropped bird with it — the subject ladder, the
per-visit detection aggregates, and `_best_detection_box` (playback/grid overlays) — so a
LEGACY stored bird box is ignored rather than half-honoured. `_subject_classes` stays
class-agnostic (it still reports the box); the consumers name what they act on.
Consequence: a swept bird-only visit now reads as a measured detection MISS (ratio 0.0).

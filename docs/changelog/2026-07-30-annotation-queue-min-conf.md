# Annotation queue floors detections at 0.3

<!-- entry 73 -->

Annotation queue (`#annotate`) now floors detections at `_ANNOTATE_MIN_CONF` (0.3): `_present_frames`
admits only yolo-serial verdicts with score ≥ floor, so the recall-first oracle's empty-frame phantoms
(conf 0.15) no longer bloat the queue + progress with junk "not a cat" visits (an empty scene isn't a
useful negative). Floors queue and progress together (shared universe); `labeled_visits` (undo/review)
stays unfloored so a decision made before the floor stays reviewable. Fixed, not per-request.

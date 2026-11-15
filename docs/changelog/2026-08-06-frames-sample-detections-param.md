# Frames sample endpoint can attach per-frame detections

<!-- entry 111 -->

`GET /api/frames/sample` gains an optional `detections=<analyzer>` param (400 if not in ANALYZER_NAMES,
count branch only) that attaches each sampled frame's stored per-frame detection for playback overlays:
`{analyzed, score, box, cls}` — the highest-confidence {cat, person, bird} box (new cv2-free
`Store._best_detection_box`, distinct from cat-only `_best_box`), or None keys when the swept frame holds
no such box; `analyzed=false` distinguishes an UN-swept frame from a swept miss. Without the param the
response is byte-identical `{id, recv_ts, url}` (no extra read), so the density/buckets viewers are
untouched. Per-frame truth (not the visit `subject` chip) so a `person` box can show during a `cat` visit.
The API/data layer; the user+admin rendering that consumes it is entry 112. Spec: docs/specs/2026-07-24-playback-yolo-boxes.md.

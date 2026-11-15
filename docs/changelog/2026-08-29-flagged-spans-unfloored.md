# Flagged spans are unfloored, unlike the queue

<!-- entry 225 -->

Flagged spans are UNFLOORED, diverging from the queue's `_ANNOTATE_MIN_CONF` (0.3):
YOLO runs recall-first at 0.15, so a faint 0.2 cat box is a real crop the queue hides.
The floor keeps phantom empty-scene detections out of the BULK queue (entry 73) — a
human pointing at one visit is not that case, and a faint crop still grades `poor`,
so a quality-filtered gallery build skips it anyway.

# admin-next Frame review page with detection and identity overlays

<!-- entry 130 -->

admin-next Frame review page (Wave 2 P3 frontend): a time-window frame grid with per-frame YOLO box +
conf + class and identity overlays drawn from `/api/frames/sample?detections=yolo-serial&identify=1`
(the P3 read); All / Detected / Identified filters and click-to-enlarge with the box + full caption.
The model-evaluation surface — a box/identity chip renders only where a frame was swept/identified.

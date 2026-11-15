# Playback renders per-frame YOLO boxes and confidence bars

<!-- entry 112 -->

Activity playback (user + admin) now renders per-frame YOLO detections: each filmstrip tile carries
a bottom confidence bar (red<40/amber/green≥65, reusing visit-health thresholds), and the played stage
frame draws the detected box (highest-conf cat/person/bird) with caption. Fed by `detections=yolo-serial`
param on `/api/frames/sample` (read-time; zero overhead without it). Box is per-frame truth, not
per-visit subject chip — a person box can appear mid-cat visit. Admin/user dashboards each own a
`bandOf` copy so they drift independently if re-tuned. Presentation only; all payload in entry 111.

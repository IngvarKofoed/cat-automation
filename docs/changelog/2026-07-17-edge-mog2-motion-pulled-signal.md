# MOG2 motion is a pulled signal, never a gate on frame delivery

<!-- entry 10 -->

Edge motion detection (MOG2 on downscaled clipped ROI in the grabber loop): motion gates
the compute's GPU cost, not frame delivery—/stream stays continuous. Motion pulled via GET /status
(camera_ok, bbox, area) and X-Motion headers on /stream parts. Locality/area gating + slow
learning + persistence reject global illumination. Config UI: overlay + tuning + Relearn.
Fixed exposure deferred; ARCHITECTURE.md updated to match the pull-signal design.

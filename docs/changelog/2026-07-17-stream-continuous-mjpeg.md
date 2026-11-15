# Edge serves /stream as continuous MJPEG from a grabber thread

<!-- entry 9 -->

Edge serves `/stream` as continuous MJPEG (multipart/x-mixed-replace) from a
background grabber thread reading at persisted fps (default 5); `/stream` and
`/frame` both serve the shared latest-frame slot with X-Frame-Id/X-Timestamp.
Config UI added Live toggle and fps control. CaptureSource.close() poisons
read-after-close to seal the device-swap race; motion gating is the next increment.

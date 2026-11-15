# Edge serves /frame via a pluggable CaptureSource and config UI

<!-- entry 2 -->

Edge tier first slice: Flask `/frame` (JPEG q90), config UI with Capture button, API (GET /api/cameras, GET|POST /api/config).
Pluggable CaptureSource (edge/capture/) with OpenCV backend; device id opaque (int or /dev/video* path) avoids lossy conversion.
FakeCaptureSource for tests. Persistent capture self-heals on failure; device switches new-before-close under lock.

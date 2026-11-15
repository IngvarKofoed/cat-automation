# YOLO predict no longer passes half

<!-- entry 178 -->

YOLO predict no longer passes `half=`: ultralytics >= 8.4 folded it into `quantize` and warns
once PER CALL whenever the key is present at ANY value, so the default logged a line per frame.
FP32 now passes no precision arg (identical — `quantize` defaults to None); FP16 passes
`quantize=16`, or legacy `half=True` pre-8.4, probed in `prepare()` (ultralytics is unpinned).
`analysis.detail` keeps its `half` key — the recorded REGIME, and stored rows read `$.half`.
Not a throughput fix: the warning measured ~4 us, noise against yolo11x@1280 inference.

# Stateless YOLO sweep batches and prefetches for throughput

<!-- entry 41 -->

Offline stateless (YOLO) sweep now batches + prefetches — a decode-ahead thread feeds the GPU
one predict() per batch, de-starving it (was ~35% util, batch=1 FP32) for ~2–4× throughput.
Windowed BSUV/MOG2 path unchanged: batching would break its rolling background.
Batching is verdict-preserving — shape-boundary chunking prevents letterbox drift, FP16/FP32 single-sourced.
New knobs: CAT_YOLO_BATCH (default 8), CAT_YOLO_HALF (FP16, off by default, cuda-only — the only lever that can move a verdict).

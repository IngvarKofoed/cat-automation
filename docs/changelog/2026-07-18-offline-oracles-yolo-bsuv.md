# YOLO and BSUV oracles validate the edge motion gate offline

<!-- entry 17 -->

Compute-tier offline oracles validate the edge MOG2 gate: YOLO (cat detector) and
BSUV-Net (background subtraction) run over stored frames, verdicts persisted to SQLite.
Background sweep job—YOLO iterates un-analyzed frames (resumable), BSUV the full time-ordered
set. Browse-UI shows disagreements (missed cats / false triggers). Heavy ML deps opt-in
(compute/requirements-analysis.txt, lazily imported); BSUV is CUDA-bound.

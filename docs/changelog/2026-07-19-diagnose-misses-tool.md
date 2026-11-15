# Read-only tool classifies gate misses and wholly-missed visits

<!-- entry 21 -->

Added `compute/tools/diagnose_misses.py`, a read-only tuning diagnostic: given the YOLO
oracle's verdicts, it classifies MOG2 *misses* (motion=0 but cat present) so a raw miss
count becomes an actionable one. It reports gate recall on cat-present frames, splits
misses by YOLO confidence (recall-first YOLO over-calls at conf 0.15 — borderline misses
may be oracle noise, not gate faults), buckets each miss by stored blob `area` vs the
thresholds to name the knob (min_area / learning_rate / max_area / persistence), and —
the load-bearing part — clusters misses into visits to separate harmless per-frame drops
from wholly-missed visits (the only misses that cost a real GPU trigger). Thresholds are
flags, not read from the Pi, so they must be confirmed against the edge's live settings.

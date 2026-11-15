# Crop-grade formula is Python, the single source of truth

<!-- entry 412 -->

The crop-grade formula is now PYTHON (`store.seed_quality`) and the single source of
truth; every visit payload carries `seed_quality` per frame and the client displays and
echoes it. Forced by the queue filtering on the grade: a filter server-side and a tally
client-side computed by two copies of one formula is a queue that can contradict its own
readout. Spec: docs/specs/2026-08-09-annotation-grade-tally.md.

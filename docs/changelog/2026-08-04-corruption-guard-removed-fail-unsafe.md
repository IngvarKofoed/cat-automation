# Edge corruption guard removed because it fails non-safe

<!-- entry 99 -->

Removed the edge motion-gate corruption guard (entry 96): a corrupt frame can still contain a real cat, so
filtering corrupt frames out of motion is FAIL-NON-SAFE. `MotionGate.process` reverts to `(motion, bbox,
area)` — no `MotionResult`/skip, `_is_corrupt` gone. Corruption is now a review-ONLY flag: the
`classify_corruption`/`corruption_thresholds` detector stays in `shared.motion`, used solely by the
`CorruptionAnalyzer` sweep + the `#corruption` page. Filtering belongs later at the GALLERY/ID, not the gate.

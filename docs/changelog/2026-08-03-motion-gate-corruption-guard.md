# Shared MotionGate skips CSI corruption frames

<!-- entry 96 -->

Shared `MotionGate` skips CSI corruption frames (thin coloured lines + magenta cast) via a per-frame
BGR-chroma test before MOG2 — no-motion, background unpoisoned, debounce streak untouched. `process()`
now returns `MotionResult(motion, bbox, area, corrupt)`; both tiers get the guard, edge logs suppression.
Thresholds are conservative fixed `_CORRUPT_*` constants (never suppress a real cat), tuned later on real
frames. Expected: an offline baseline re-run over pre-guard frames diverges from stored `frames.motion` on corrupt frames. Spec: docs/specs/2026-07-23-corrupt-frame-motion-guard.md.

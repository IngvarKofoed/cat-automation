# Edge and compute instantiate one shared MotionGate core

<!-- entry 22 -->

Single source of truth — edge and compute instantiate shared `MotionGate` (post-transform MOG2 core:
downscale → gray → threshold → morph → largest blob → area gate → debounce). Edge's refactor is
behavior-preserving; kills the "second MOG2 drifts" risk.

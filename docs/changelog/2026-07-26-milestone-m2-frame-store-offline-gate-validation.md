# Milestone M2 — Frame store + offline gate validation

<!-- milestone M2: entries 16–56 -->

**M2 — Frame store + offline gate validation** (16–56). Compute stores every edge frame in a
byte-capped, SQLite-indexed ring (WAL, `synchronous=NORMAL`) and validates the edge gate offline:
`yolo-serial` is the trusted oracle, batched `yolo` over-detects, BSUV is CUDA-bound, and `MotionGate`
is shared so an offline re-run cannot drift from the live gate. `gate_scorecard` headlines **visit**
recall — one caught frame wakes the GPU, so a wholly-missed visit is the only miss that costs a
trigger. Sweeps drain a serial FIFO queue, resumable and cancelable.

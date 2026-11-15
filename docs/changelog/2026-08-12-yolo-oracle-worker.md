# Always-on YOLO oracle worker keeps full-coverage verdicts current

<!-- entry 138 -->

Always-on YOLO-oracle worker keeps FULL-coverage (motion + non-motion) `yolo-serial` verdicts pre-computed, so a
motion-tuning scorecard no longer waits on a per-day manual sweep. New `YoloOracleManager` ticks every 30s over the
un-swept tail through the SAME `run_analysis` path (verdicts byte-identical to a manual sweep), in 256-frame chunks
re-checking `is_busy` between them — `run_analysis` honors only `stop_event`, so a chunk boundary is the ONLY place a
mid-tick operator job can win. Detect-only, so it needs no promoted gallery. Toggle: Start page "YOLO all".
Spec: docs/specs/2026-07-26-yolo-oracle-worker.md.

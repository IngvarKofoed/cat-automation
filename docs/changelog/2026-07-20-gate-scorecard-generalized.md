# Gate scorecard works across any motion source and oracle

<!-- entry 24 -->

Gate scorecard generalized across motion sources (live or offline re-run) and oracles.
Computes recall, missed frames (source-still ∧ oracle-present), false triggers (source-motion ∧
oracle-absent), misses split by oracle confidence, area-vs-knob buckets (diagnoses which param),
visit clustering (wholly-missed visits cost GPU). Fidelity check (baseline vs frames.motion) validates
method transfer. Subsumes diagnose_misses.py into Store.gate_scorecard.

# Scorecard gains a tunable oracle confidence floor

<!-- entry 72 -->

Gate scorecard gains a tunable oracle-confidence floor (`gate_scorecard(oracle_floor=)` store-default 0;
`/api/tuning/compare?oracle_floor=` default 0.30; "oracle conf ≥" field on #tuning). "Present" is now
verdict=1 AND score ≥ floor — re-slicing the SAME stored verdicts, no re-sweep; floor 0 = old scorecard.
Why: YOLO runs recall-first at conf 0.15 and hallucinates cats on empty frames, so phantoms inflated
present/missed and fragmented into thousands of bogus visits. Caveat: metrics below ~0.3 are phantom-dominated.

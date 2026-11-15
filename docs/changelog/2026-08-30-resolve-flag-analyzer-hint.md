# _resolve_flag de-indexes analyzer to avoid a mis-plan

<!-- entry 229 -->

`_resolve_flag` hints `+analyzer` to de-index that term. With no ANALYZE stats SQLite
preferred `idx_analysis_analyzer_verdict` over the `frame_id` range and scanned every
verdict for the oracle — O(analysis) PER FLAG, the opposite of the per-span read the
design chose. Measured at 1.5M verdicts: 65 ms → 0.4 ms, flat in store size.
The same query shape elsewhere (e.g. `events()`) still carries the mis-plan.

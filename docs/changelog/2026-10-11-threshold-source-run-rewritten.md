# threshold_source_run_id is rewritten on every threshold write

<!-- entry 438 -->

`metrics.threshold_source_run_id` is rewritten on every `set_model_threshold` and nulled
when no run is named. It was only ever written, so a later hand-set value carried the
earlier run's id — attributing an operating point to grades and exclusions that produced
a different one. A stale justification is worse than the absent one the stamp was added
to fix. `threshold_built` still copies once, and is unaffected.

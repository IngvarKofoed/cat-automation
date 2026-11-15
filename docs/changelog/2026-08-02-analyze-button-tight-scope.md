# Analyze scopes to loaded events and motion frames only

<!-- entry 91 -->

Activity **Analyze** button is now tight + fast (buckets/Sweeps stay the breadth tool). It scopes to
the LOADED events' bounding id-span (min start_id .. max end_id), not the whole date window, and sends
a new opt-in `motion_only` so the sweep skips the non-motion majority (~95% at continuous capture).
`motion_only` threads through `/api/analysis/run` → `enqueue_named` → `run_analysis` →
`iter_unanalyzed`/`count_unanalyzed` (add `frames.motion=1`); it's in the job dedup key so a tight vs
full sweep of the same window don't collide. The `reanalyze` clear is motion-scoped under `motion_only`,
so the tight button re-detects the visits' motion frames WITHOUT wiping non-motion verdicts a breadth
sweep produced. Default off everywhere → every existing sweep path is byte-identical.

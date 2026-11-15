# Admin Activity gains an Analyze re-detection button

<!-- entry 90 -->

Admin Activity page gains an **Analyze** button (left of Identify) that enqueues a `yolo-serial`
re-detection (`reanalyze=true`) over the shown date window, so historical events backfill their
person/bird/cat subjects — the DETECT step, vs Identify's MATCH step. `reanalyze` is required
because a plain sweep skips already-analyzed frames; the old cat-only rows must be cleared and
re-detected by the broadened detector. Reuses `/api/analysis/run` + the window resolver; progress
lives on Sweeps (the button reflects a running yolo-serial sweep). Forward path stays the live
worker; this is the manual backfill for frames scored before the detector was broadened.

# Optional motion-only capture drops non-motion frames

<!-- entry 32 -->

Optional motion-only capture mode (compute-side filter, default off): drops non-motion frames to save disk.
Toggleable live via motion_only setting. Mode transitions recorded in mode_changes table with frame id + ts.
Caveat: misses unmeasurable when motion-only is on; buckets/timelines flag "misses unmeasurable here" if overlapping a motion-only span.

# Eviction reclaims non-motion frames first outside tuning

<!-- entry 303 -->

Eviction is stage-aware: outside `tuning` it reclaims non-motion frames FIRST, so the same
disk holds annotatable motion history much further back. `tuning` stays byte-for-byte
plain oldest-first — note non-motion frames are NOT spared there, merely never
preferentially targeted. This is the auto-cleanup: driven by disk pressure, so no stage
change is destructive and a leftover keep-all window is shed gradually, not wiped.

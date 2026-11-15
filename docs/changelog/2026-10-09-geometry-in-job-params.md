# Geometry joins job params and artifact slug for both kinds

<!-- entry 431 -->

Geometry joins the job `params` for BOTH kinds, so the two arms of a crop-shape A/B are
two jobs rather than one deduped press, and lands in the artifact dir slug. Canonicalised
first (`m10` == `m10.0`), or two spellings would dedup apart and claim two dirs for one
crop set. `count_identified_crops` gained the same filter, since a pre-check that
counted crops the build then discards would wave through a build that finds nothing.

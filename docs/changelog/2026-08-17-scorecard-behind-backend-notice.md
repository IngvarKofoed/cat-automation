# A scorecard without params says the compute build is behind

<!-- entry 164 -->

A scorecard whose compare response carries NO `params` now says so ("this compute build
predates the per-column params field; pull + restart it") instead of silently rendering
nothing, which read as the feature being absent. Distinct from a slot that HAS no recorded
params because it never re-ran. The case is real, not hypothetical: the dev proxy serves a
LOCAL admin-next against a REMOTE compute, so the page routinely runs ahead of its backend.

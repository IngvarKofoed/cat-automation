# Annotation gained a Labelled review mode

<!-- entry 196 -->

Admin `#annotate` gained a Labelled mode: review decided visits newest-first, filter by label
(incl. `ignored`), re-label with the same 1-9/u/x keys, or `d` one back to the queue. Fixing a
mislabel no longer means the retired `/admin-old` — it closes P4's deferred relabel item.
A re-label now RE-SEEDS each crop's quality grade with the queue's own formula; passing the
stored grade through left a not-a-cat correction ungraded, so every gallery build skipped it.

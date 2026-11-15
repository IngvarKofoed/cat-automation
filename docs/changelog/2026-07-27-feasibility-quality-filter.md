# Feasibility probe filters by crop quality grade

<!-- entry 60 -->

Feasibility probe gains a `--quality gallery[,ok[,poor]]` filter (new `qualities` arg on
`Store.labeled_crops`): A/B gallery-only vs all-crops to test whether crop quality — not the
cats — is the separability bottleneck, answering the report's "weak ≠ hopeless" hedge with a
measurement. Filtered runs write to `feasibility-<slug>` (grade stamped in report + console) and
exclude NULL-grade crops; default (no flag) unchanged. Grades still have no other consumer.

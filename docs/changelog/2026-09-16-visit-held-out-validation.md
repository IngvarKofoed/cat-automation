# Validation reports a visit-held-out accuracy using Run's own voter

<!-- entry 316 -->

Validation now reports a VISIT-held-out number: each visit is hidden whole, matched
against the other visits, and named by Run's own rule (`Store._aggregate_identity`,
injected so the probe cannot drift from what Run decides). Outcomes are correct /
wrong / **declined** — kept apart because for a resident at the door those mean
opposite things. The report leads with it; crop-level kNN stays, demoted, as the one
number comparable with earlier runs.
Spec: docs/specs/2026-08-03-visit-held-out-validation.md.

# Labelled never refreshes cross-client, accepted limit

<!-- entry 519 -->

Known limit, accepted: `labelled` never refreshes CROSS-CLIENT. `activity_signal`
carries no `dataset_items` term (entry 285's reasoning), so a desk requeue leaves a
stale ✓ on an open phone feed and "Skip done" steps past that visit until the next feed
load. The harmful direction is exactly that one; the benign one (a new label) self-heals
the same way. Fix, if it bites: a label revision counter in `activity_signal`.

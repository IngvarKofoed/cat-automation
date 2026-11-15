# Validation measures stranger rejection by holding each cat out

<!-- entry 423 -->

Validation can now measure STRANGER REJECTION, which nothing could before: every crop
the probe scores belongs to a labelled cat, so "how often is an unenrolled cat given
one of our names" had no number at all. Each cat is held out of the gallery in turn
via the existing `gal_mask`; its visits then score *unknown* as CORRECT. Impersonations
split resident vs neighbour — only the first matters at the door.
Spec: docs/specs/2026-08-09-open-set-scoring-and-calibration.md.

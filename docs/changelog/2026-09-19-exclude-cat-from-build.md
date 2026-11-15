# A cat can be left out of one build without being retired

<!-- entry 330 -->

A cat can be left out of ONE gallery build without being retired: the Model page's new
shared checkbox list sends `exclude_cat_ids` on build AND validate. Retiring was the
only mechanism and is the wrong one — it also drops the cat from the annotation picker,
stopping the labelling the exclusion is waiting for.
Spec: docs/specs/2026-08-03-gallery-build-cat-exclusion.md.

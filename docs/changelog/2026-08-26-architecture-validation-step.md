# ARCHITECTURE learning loop matches code on validation

<!-- entry 209 -->

ARCHITECTURE's learning-loop section now matches the code: validation is its own step
BEFORE training (it scores the labelled data, reads no gallery, belongs to no version,
and is skippable), a built version carries its own threshold + separability from the
vectors it enrolled, and promotion covers delete + the one-active invariant.
It had claimed "a new version is validated, then promoted" — per-version validation,
which does not exist. Recorded as deliberately-not-done so it isn't read as a gap.

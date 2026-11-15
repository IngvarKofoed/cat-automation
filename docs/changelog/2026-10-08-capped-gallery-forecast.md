# Validation forecasts a capped gallery with recalibrated threshold

<!-- entry 427 -->

A validation run now forecasts a CAPPED gallery — `cap_per_cat`'s selection as a
`gal_mask` over the same matrix, several caps per run — and recalibrates the threshold
under each mask. Capping is meant to fix both biases `cap_per_cat` names; reusing the
uncapped threshold would answer only the density half. Both columns are reported so
which half moved is visible rather than inferred.

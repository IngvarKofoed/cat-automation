# model_versions.threshold is settable without a rebuild

<!-- entry 425 -->

`model_versions.threshold` is now SETTABLE (`POST /api/training/models/{id}/threshold`,
plus a Model-page field). It was written once by `build_gallery` and only ever read, so
the operating point could not move without a rebuild — and the active model declines
0 of 515 visits, i.e. the open-set fail-safe is calibrated off. Applied at read, so a
change needs no re-identify and RESTATES all history; the UI says so in as many words.

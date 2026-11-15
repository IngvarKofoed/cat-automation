# Crop deletion globs every geometry variant

<!-- entry 448 -->

Keeping them broke an invariant two other paths relied on: a crop file may outlive a
geometry MOVE but never its ROW. `_delete_crop_files` removed only the row's current
`crop_path`, so a relabel left the other shapes' files orphaned and a later hop would
`relink` one cut from the PREVIOUS bbox — stamp, bbox column and pixels disagreeing, with
nothing downstream able to notice. It now globs every variant.

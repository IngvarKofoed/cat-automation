# Geometry compare-and-swap gained bbox

<!-- entry 449 -->

`update_dataset_geometry`'s compare-and-swap gained `bbox`. Path alone could not catch a
relabel to the SAME cat: it re-commits at the identical legacy path, so id and path both
still match a row the run never read. The box is the exact predicate — it is what must be
unchanged for the cut pixels to still belong to that row. Compared with `IS`, so a NULL
box is null-safe rather than matching nothing.

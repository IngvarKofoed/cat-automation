# A settling re-label repaints its own tile

<!-- entry 246 -->

A settling re-label repaints ITS OWN tile (`refreshTileChip(v)`), not the selected one. Same
root cause: the write lands long after the keypress, so if the operator has moved on, the
tile they just corrected kept its old cat until a full rebuild — in the grid whose whole
job is showing labels at a glance.

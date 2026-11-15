# Frame review and Activity share one overview tile

<!-- entry 153 -->

Frame review and Activity now render ONE shared overview tile — same fonts, same 12px chips,
same box colours — so the two grids can't drift. Detection boxes and labels use the USER
dashboard's traffic light (red <40% · amber <65% · green) and its identity palette, duplicated
into admin-next per the entry-80 no-shared-CSS convention: change them together.
The YOLO label distinguishes "no det" (swept, nothing found) from "unswept" (never analysed).
Chips sit in a WRAPPING row, not pinned to opposite corners, which silently overlapped on a
narrow tile or a long cat name.

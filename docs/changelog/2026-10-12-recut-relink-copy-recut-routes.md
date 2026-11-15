# Recut routes rows three ways: relink, copy, recut

<!-- entry 446 -->

Cutting is now the LAST resort, not the only path. `recut` routes each row three ways:
**relink** (a file already sits at the target path — move the stamp, touch no pixels),
**copy** (another kept file carries the target's MARGIN, so the pixels are right and only
the path is wrong), **recut** (decode the frame). Only the last needs a bbox and a live
frame, so a `letterbox`-only flip strands nothing — it alters no pixel. For a fixed
margin exactly TWO geometries exist (letterbox on/off), so finding a pixel-equal file is
two stats, not a directory walk.

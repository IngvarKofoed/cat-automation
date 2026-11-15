# canSeed and its older-build banner are gone

<!-- entry 416 -->

`canSeed` and its "your compute PC is on an older build" banner are GONE with the
client formula. They existed because the client could not grade a payload lacking
per-frame scores; the server always has the score and the peak area, so the state
they warned about is now unreachable.

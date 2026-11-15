# Queue tables share one fixed column geometry

<!-- entry 151 -->

admin-next queue tables share one fixed column geometry (`table-layout:fixed` + a `<colgroup>`):
Name widest, then Frames, then Status, with % done / FPS / ETA narrowest. Auto-widths had sized each
card's table to its own rows, so the same columns landed in different places per card. "Time to
complete" is now "ETA". Cleanup's Drop/Sweep lost their `…` and share one width. Presentation only.

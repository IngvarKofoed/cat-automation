# Saved flash overwritten and label-state line broke nav layout fixes

<!-- entry 488 -->

The other two were browser-only. `renderSaving()` ran AFTER `flashSaved()` and
overwrote the green "Saved" with the empty state, so a drained queue looked identical
to one that never appeared — the exact thing entry 296 added the flash for. And
`flex: none` on the new label-state line pushed the visit nav onto its own row on a
wide screen, undoing what `.msg { flex: 1 }` is documented to do; it shrinks with an
ellipsis now, full text in `title`.

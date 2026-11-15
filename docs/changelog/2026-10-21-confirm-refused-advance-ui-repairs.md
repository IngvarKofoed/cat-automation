# Confirm repairs flag repaint, busy paint and probe sequence guard

<!-- entry 491 -->

Three UI-state repairs to the confirm flow, all in the refused-advance case (a
confirm on the last loaded visit, where `moveEvent` declines and the visit stays on
screen). The ⚑ button now repaints after a confirm clears the flag — stale, its next
tap took the ADD branch and re-created the mark (entry 228's class); the button paints
its busy state before the hop rather than after the write; and `probeConfirm`'s
sequence guard now gates the STATE WRITE, not just the paint, so a slow first probe
could no longer overwrite the fresher post-write answer and bring the button back on
an already-labelled visit.

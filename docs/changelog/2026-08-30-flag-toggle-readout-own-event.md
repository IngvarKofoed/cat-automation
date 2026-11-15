# Flag toggle reports only while its own event is on screen

<!-- entry 228 -->

The user app's flag toggle reports onto the modal readout ONLY while its own event is
still on screen, and re-enables the button for whatever IS. Prev/Next repoints that
shared button at another event, so a settling write repainted a lie there — "Marked ✓"
over an unflagged visit, whose next tap then ADDED a mark the user meant to remove.
A failed write also restores the flag delta, not a whole snapshot that could undo a
flag made elsewhere meanwhile.

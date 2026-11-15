# Player keys still label; Space plays and pauses

<!-- entry 292 -->

With the player open the keyboard still labels: a bound key closes it and then does
its normal thing, so a decision always lands on the visit that was on screen (the
player only ever plays the staged visit, and nothing navigates without closing).
Space is the exception — play/pause, since skipping a visit because the operator
reached for pause is the worse surprise. Any stage repaint closes it, and so does a
mode SWITCH: that load is async and leaves the outgoing mode's list in place, so a
key pressed over the player would have re-labelled a stale, already-decided visit.

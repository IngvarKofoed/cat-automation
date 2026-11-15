# Queue renders are mode-guarded

<!-- entry 219 -->

Queue renders are MODE-GUARDED. A write settles long after its keypress now, so a chained
callback could paint the queue over Labelled review — leaving the stage showing one mode's
visit while the keys dispatch to the other's, which re-labels a visit the operator cannot
see. `loadQueue` also filters locally-decided frames PER FRAME, not per visit: a returned
cluster can mix decided frames with fresh ones, which dropping it whole would hide.

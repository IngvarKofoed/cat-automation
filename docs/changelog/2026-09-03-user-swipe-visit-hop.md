# Horizontal swipe on the frame hops visits

<!-- entry 251 -->

A horizontal swipe on the played frame does the same hop, with the frame tracking the finger
— so the gesture is self-teaching rather than a silent jump. Swipe left = older, matching the
newest-first feed. Damped to a third at either end.
The ease-back is the "that didn't take" cue, so it plays ONLY when the visit doesn't change
(short drag, refusal at an end, cancel); a committed hop drops the transform instantly, since
animating it back would read as the gesture bouncing off the swipe that worked.

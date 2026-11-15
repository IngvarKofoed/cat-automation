# A drained queue confirms with a green Saved

<!-- entry 296 -->

A drained queue now CONFIRMS with a brief green "Saved" instead of the chip merely
vanishing, which looked identical to one that never appeared. Suppressed when the
batch held a failure or a short write, so it can never contradict the error line
those already print.

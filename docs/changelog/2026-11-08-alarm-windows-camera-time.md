# Start windows are measured in camera time

<!-- entry 579 -->

Every start window is CAMERA time, measured from the newest counted frame's `recv_ts`: a
pass takes 1-4 s with a cat in view, so wall-clock windows lost ~2 s of frames and a
tick-counted hold would cost a whole pass. Wall-clock age speaks only when the pass reports
`caught_up` — the gone check's own condition — which also lets a short visit's hold finish.

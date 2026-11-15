# One low-priority encode at a time, killed if phone leaves

<!-- entry 545 -->

One encode at a time, process-wide, at below-normal priority with the decoder, scale filter and
x264 held to 1/1/2 threads — the collector, workers and alarm tick share this CPU. A phone that
leaves (aborted fetch) has its encode killed. A request finding the lock held waits up to two
disconnect polls (1 s) before its 409: an immediate tap after a hop otherwise met "another
clip" about the abandoned one, since the route only sees a disconnect at its next 0.5 s poll.

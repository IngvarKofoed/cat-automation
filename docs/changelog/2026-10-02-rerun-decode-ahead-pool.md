# MOG2/BSUV re-run decodes ahead on a small thread pool

<!-- entry 397 -->

The MOG2/BSUV re-run decodes AHEAD on a small thread pool while inference stays
strictly serial. Its rolling background needs every frame in order, but decode is a
pure function of the file, so consuming futures in submission order feeds the analyzer
the identical sequence. Decode is 74% of a serial iteration (4.79 ms vs the gate's
1.71 ms at 1280x720 q90), so 2 workers took the re-run 166 -> 332 frames/s: one day of
capture ~88 -> ~44 min per slot, and a tune compares two slots.

# Flagged visit state derives from coverage not verdicts

<!-- entry 226 -->

A flagged visit's state is derived from COVERAGE (`n_swept` vs `n_live`), never from a
verdict merely existing — partial sweeps are the norm (entries 76, 142, 149), and
calling one `no_detection` would claim the detector rejected frames it never saw.
Analyse is offered only where coverage is incomplete, and sweeps the span WITHOUT
`motion_only`: coverage counts every live frame, so a motion-only sweep could never
clear `partial` on a keep-all store.

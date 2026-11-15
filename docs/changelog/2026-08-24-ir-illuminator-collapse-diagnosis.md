# IR illuminator collapses at civil dusk, diagnosed not fixed

<!-- entry 199 -->

Diagnosed (not yet fixed): the IR illuminator collapses to a few percent output at civil
dusk and holds there till dawn, so EVERY night captured so far is near-dark from ~22:15.
Do not tune night MOG2 params, calibrate the lighting cutoff, or build night IR crops from
that data — a bad night scorecard reflects a broken lamp, not the gate. Leading cause is
thermal (3 W emitter, half-enclosed back). Separately, `catpi` runs permanently under-volted.

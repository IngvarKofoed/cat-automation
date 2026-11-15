# Pinned scorecard measured 126.7 s to 5.7 s per Compare

<!-- entry 392 -->

Measured on a 6M-frame / 13.5M-verdict replica over one 750k-frame day: one Compare
click 126.7 s -> 5.7 s, with every card's visit and missed count identical.
The honest figure is the VDBE-step ratio, ~3x (live 214M -> 63M, a slot 234M -> 81M):
wall clock flatters it to 22x because the old plan also random-probes rowids across
the whole file, which this cache-starved dev box punishes far harder than the compute
PC will.

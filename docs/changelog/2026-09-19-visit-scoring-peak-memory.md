# Visit scoring raises peak memory; matrix min/max no longer copies

<!-- entry 329 -->

Measured, correcting the spec: visit scoring DOES raise peak memory (n=6000: 2109 →
2439 MB), via two pair-length `int64` gathers for the cross-visit mask. The largest
addition is gone — finding the matrix min/max no longer boolean-index-copies the whole
matrix (~1.16 GB at 12k crops), since `1 - (unit @ unit.T)` is always finite.

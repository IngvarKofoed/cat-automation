# parse_geometry rejects non-finite margins

<!-- entry 473 -->

`parse_geometry` now rejects a NON-FINITE margin. `< 0` does not catch inf or nan
(both compare False) and `%g` renders them back as `minf`/`mnan`, so such a stamp
round-tripped as valid and reached the cutter: margin=inf raises OverflowError inside
`_clamp_box`, which `materialize` does NOT catch — a 500 on EVERY label — and nan
returns False for every frame, so a label silently writes no rows. Guarded at the
parser both the request path and the stored-value read go through, not at one route.

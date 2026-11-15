# An unavailable run no longer reads as an older compute build

<!-- entry 369 -->

A run that RAN and reported `available:false` (no cat with two visits — an ordinary
early-dataset state) no longer draws the "compute PC is on an older build" stamp: it
is a data state, re-running Validate reproduces it, and the message sent the operator
after a deployment problem instead of "label more distinct visits". The older-build
wording now fires only when no run carries a visits block at all.

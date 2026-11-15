# min_frames floors on frame count, not gallery-grade frames

<!-- entry 404 -->

It floors on frame COUNT, deliberately not on "has a gallery-grade frame" — the
predicate a build actually enrols on. Computing that server-side needs a second copy of
the client's `seedQuality`, and that formula has a known defect: a one-frame visit's
area ratio is 1.0 BY CONSTRUCTION (the frame is its own peak), so its area test cannot
fail and it grades `ok` structurally. Revisit the predicate once the seed is fixed.

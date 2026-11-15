# Day/night stays sun-times driven, lighting cutoff unset

<!-- entry 272 -->

Day/night stays SUN-TIMES driven and the lighting cutoff is deliberately left unset.
The IR lamp runs on the edge's astronomical schedule (sunset−1min / sunrise+1min) from
the SAME lat/lon the compute split uses, so the two agree by construction — the spec's
premise, an illuminator whose photocell drifts from sun times, is not this wiring.
And were MOG2 params ever to diverge, the switch must happen LIVE ON THE EDGE, which an
offline read-time flag could not drive regardless of how accurate it got.

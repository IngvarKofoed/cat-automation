# Verification found a hollow flag test and two browser-only UI bugs

<!-- entry 487 -->

Three defects the verification found, each invisible to the other half. A test asserting
the ⚑ survives `inserted: 0` PASSED against an unconditional flag delete — its span had
evicted, so the route 409'd at `no_crop` and never reached the flag code; it now removes
the JPEGs and keeps the rows, which is the only way to be confirmable and still insert
nothing. Entry 411/433/466/472's hollow-test trap, fifth instance, same tell.

# Scorecard plan pinning is enforced by a test

<!-- entry 396 -->

Plan pinning is now a TEST, not a comment: `test_scorecard.py` asserts each scoped
query's plan seeks the frames id range and each unscoped one does not. Nothing else
can catch a regression here — the numbers are identical either way, which is how this
class has recurred five times. Verified to fail against the reverted pin, and the
default plan is wrong even on a 12-frame store, so it needs no big fixture.

# Unavailable visit block carries no auc or balanced-accuracy threshold

<!-- entry 327 -->

An `available: false` visit block carries NO `auc`/`threshold_balanced_acc`. Both are
computed before the availability branch, so they leaked past "nothing was measured" —
the exact distinction the visit scoring exists to keep honest.

# The kicker caption class is no longer scoped to cats-section

<!-- entry 352 -->

`.kicker` is no longer scoped to `.cats-section`: the Visits tally card uses the same
caption beside its own `h2`, where the descendant rule never applied and it rendered
as bold heading text (measured 18.4px/700 vs the intended 13.6px/600 muted).

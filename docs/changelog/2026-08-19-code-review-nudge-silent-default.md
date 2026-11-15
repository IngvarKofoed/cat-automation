# The code-review nudge defaults to silence

<!-- entry 176 -->

The root CLAUDE.md `/code-review` nudge now DEFAULTS TO SILENCE, instead of firing on any
"significant" change at a hardcoded `medium` — gated on an adjective it fired nearly every
turn, so the suggestion carried no signal. It fires only on a NAMED trigger (blocker fixed,
high-consequence surface, large non-mechanical diff, multi-agent diff, uncertainty), must say
which one, and picks `medium`/`high` by rule. `shared/` is also now described as holding
cross-tier LOGIC (`MotionGate`, wire format), not contracts alone.

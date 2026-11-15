# An empty window returns zeroed stats, never an unbounded read

<!-- entry 343 -->

A `since_id` of None means the window holds no frame — returned as the zeroed shape,
never as an unbounded `events` read, which would count the newest visits in the
WHOLE store as though they fell inside the window. Reachable whenever the newest
frame predates the window (a stopped collector).

# Two geometry review findings left unfixed by decision

<!-- entry 440 -->

Two review findings left UNFIXED by decision, so they are not re-litigated as new. A
letterbox-only flip strands crops whose source frames evicted — `letterbox` is a
read-time choice touching no stored pixel, yet the tool needs a live frame to move the
stamp; the honest fix is to stop stamping it on a file at all, which is a redesign worth
taking BEFORE a second geometry arm ships. And the probe CLI's pre-count print
overstates on a mixed-geometry store; the probe's own count is authoritative.

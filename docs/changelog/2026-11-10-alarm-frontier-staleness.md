# Staleness and hold are measured to the processing frontier

<!-- entry 589 -->

Review repair: staleness and the hold are measured to the step's processing FRONTIER —
now when caught up, the last processed frame's `recv_ts` when behind — replacing the spec's
`caught_up` flag. "Behind = never stale" let a foreign presence up to 2 min old (switch
turned On, or frames named later) meet its hold in camera time and sound at the NEXT cat
to arrive. Frames still to come lie after the frontier, so they cannot continue it.

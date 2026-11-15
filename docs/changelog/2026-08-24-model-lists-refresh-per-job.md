# Model page lists refresh once per finished job

<!-- entry 201 -->

The Model page's version + validation lists refresh once per FINISHED job instead of on
every 3s idle poll (two whole-table reads on the shared store connection, for data that
cannot change while the manager idles). "Finished" needs BOTH signals: running→idle misses
a job that completes with the next already promoted behind it, and a changed `result`
misses a canceled/failed one, which leaves the prior result untouched.

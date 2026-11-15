# Edge fps is set to 15 but the Pi grabs about 10

<!-- entry 275 -->

Edge `fps` is set to 15 but the Pi only grabs ~10.2; on 30 July it ran 7.7–9.4 with no
outage. Nothing is lost in transit (compute stored 10.24 against the Pi's 10.19), so the
Pi is the ceiling — though a stored-rate dip can equally be collector lock contention,
which frame-id continuity cannot distinguish (ids are assigned at insert, so a frame
dropped before insert leaves no gap). Matters for tuning: `persistence` counts FRAMES,
so 2 is a ~250 ms window here, not the ~400 ms it would be at the documented 5 fps.

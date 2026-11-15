# Alarm votes over the bridged household visit so far

<!-- entry 598 -->

The alarm votes over the household visit SO FAR (`Store.visit_start`), not a 5 s-gap presence;
`ALARM_PRESENCE_GAP_S` is retired. A sit no longer restarts the vote (Saffi, 9/11 18:55, lost
144 frames to one). The read is floored at the vote's own floor, so it stays 0.4 ms however long
a cat naps; the row's `visit_start_ts` is one unbounded walk at start. Stored trigger stays `presence`.

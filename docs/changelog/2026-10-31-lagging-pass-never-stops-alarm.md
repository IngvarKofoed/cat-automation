# Review items left unfixed: lagging pass never stops alarm

<!-- entry 540 -->

Left unfixed from that review. A pass that can't keep up never stops the alarm, and in
Tuning — where frames arrive continuously — that means never, not "while motion lasts"
as the spec put it: needs a design decision. Closed naming waits as long as motion lasts,
and failed clips retry every tick: both by spec. `open_visit` re-walks the open cluster
per call — measured 3.9 ms at 30 min, 15 ms at 2 h; a cache needs Clear/eviction care.

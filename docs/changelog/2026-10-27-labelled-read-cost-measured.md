# Per-span labelled read measured at 0.1 ms per page

<!-- entry 518 -->

Measured the added per-span read rather than quoting only its plan: 0.1 ms per
100-event page on a 2M-frame / 666k-verdict / 33k-crop replica with no `ANALYZE`,
against 0.4 ms of existing per-span work on the same page. Sub-millisecond under the
shared write lock, so it is nowhere near entries 102-105/338's starvation class.

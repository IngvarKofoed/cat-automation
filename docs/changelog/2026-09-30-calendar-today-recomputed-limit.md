# Today recomputes every load while the collector runs

<!-- entry 386 -->

Known limits, both accepted. While the collector runs, TODAY is invalidated ~10x/s
and so is recomputed every load (~1.4 s for a full day on the real store); it
memoizes like any other day once collection stops. And the first load after this
ships is still a cold one — the memo is persisted, so a restart does not re-pay it.

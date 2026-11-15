# Automatic launch sweep made the slow probe a real starvation risk

<!-- entry 308 -->

That was latent while the sweep was manual and cancelable — entry 306 made it automatic at
every launch, which would have pegged the store lock for hours and starved the collector on
the real store. Caught by measuring before upgrading, not by the test suite: correctness was
never wrong, only the plan. An unparseable filename falls back to the slow scan rather than
being assumed orphaned.

# Worker on/off intent survives a clean restart

<!-- entry 140 -->

Worker on/off intent now survives a clean restart: `stop()` gained `persist` (default True) and the shutdown hook passes
`persist=False`. The hook's `stop()` previously wrote the OFF intent on every clean exit, so the launch-time `restore`
could never fire — fully exposed by the new oracle, latent in `live_identify` (masked by its
auto-start-when-a-model-is-promoted clause). An operator stop still remembers the off.

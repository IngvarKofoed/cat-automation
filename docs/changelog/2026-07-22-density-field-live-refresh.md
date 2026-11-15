# Density-rate field refreshes the preview on input

<!-- entry 35 -->

The density-rate field refreshes the preview live. It now reloads on `input` (debounced), not `change`,
so a typed "frames / min|hour" updates the grid and count immediately instead of only on blur/Enter.

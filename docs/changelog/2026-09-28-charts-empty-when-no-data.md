# Charts return empty when their data is absent

<!-- entry 376 -->

Both charts return "" when their data is absent (no per-cat rows, no day/night without
a location) and the block skips a chart it has no PNG for, so a location-less run
shrinks the summary rather than drawing one regime as if it were both.

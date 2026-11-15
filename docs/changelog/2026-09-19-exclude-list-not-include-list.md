# Cat selection is an exclude-list, per build, never persisted

<!-- entry 331 -->

It is an EXCLUDE-list, never an include-list, so absent/empty means "enrol everyone"
and a cat added to the roster later is enrolled by default — an include-list would
silently drop a new resident from every repeated build. Per-build only, never
persisted: a reload re-ticks everyone.

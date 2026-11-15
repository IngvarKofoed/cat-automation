# door_stats pages events on the shared write connection

<!-- entry 354 -->

Known limit, left deliberately: `door_stats` pages `events()` on the store's SHARED
write connection, unlike `tuning_calendar`/`lighting_histogram`/`labeled_visits`,
which hold their own short-lived WAL connections for exactly this reason. With the
Visits page open, the SSE nudge refetches every ~3 s while a cat lingers. Two
defensible fixes — a read connection under `events()` (wide blast radius: Activity
and `cats_overview` share it) or debouncing the client refetch — so it needs a
decision, not a patch. Per-span reads are ~5 ms, so this is added latency, not the
19.5 s class of entries 102-105.

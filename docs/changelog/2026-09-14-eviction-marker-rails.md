# Eviction marker rails: reset on clear, no set_setting, rollback re-read

<!-- entry 305 -->

Three rails that marker needs. `clear()` RESETS it — the settings KV survives a wipe while
frame ids restart at 1, so a stale value would banner a fresh store as unmeasurable
(entries 141/143/144, a fourth direction). It is never written via `set_setting` (which
takes the store lock `_evict_locked` already holds — a non-reentrant deadlock), riding the
caller's transaction instead so it can't diverge from the deletes. And `add`'s rollback
re-reads it, or a discarded write would leave it ahead in memory and REGRESS on restart.

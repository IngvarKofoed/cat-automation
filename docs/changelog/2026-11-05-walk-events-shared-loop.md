# Keyset loop extracted as Store._walk_events

<!-- entry 564 -->

`door_stats`' keyset loop is now `Store._walk_events(since_id, max_pages, with_subject)`,
shared with the daily count, carrying entries 271 and 350's rules once. No behaviour change
to Visits; its tests pass unmodified.

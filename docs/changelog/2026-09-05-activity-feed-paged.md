# Activity feed is paged at 100 visits per page

<!-- entry 259 -->

The Activity feed is PAGED, 100 visits per page (`/api/events` gained `limit`), on both
dashboards. Cost scales with the page, so page 1 went ~54 ms → ~20 ms; 500 was never a
chosen page size, just the store's `_MAX_EVENTS` cap. Below ~50 the gain flattens against
the feed's fixed floor, so 100 is the knee.
`Store.events`'s own default stays wide — `cats_overview` walks the feed for the newest
event naming each cat, so a 100-event window would shorten every "last seen" it reports.

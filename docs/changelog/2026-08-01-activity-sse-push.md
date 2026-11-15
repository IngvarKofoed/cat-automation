# Activity feed is pushed over SSE on motion-scoped changes

<!-- entry 87 -->

Live push for the Activity feed over SSE (`GET /api/events/stream`): the server nudges connected
clients when the feed actually changes, so a foregrounded dashboard updates in near-real-time instead
of waiting for the poll. Signal is `Store.activity_signal()` — MAX motion-frame id (new door event),
MAX identifications rowid (a late naming), and the active model id (a promotion). Motion-SCOPED on
purpose: continuous frame capture would fire a whole-store "newest frame" signal every tick.
Client opens the stream while visible, tears it down when hidden; the 60s poll (86) stays as a
fallback for when SSE can't connect (unsupported client, buffering proxy).

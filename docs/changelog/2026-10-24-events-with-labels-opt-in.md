# Store.events gains opt-in with_labels boolean per event

<!-- entry 503 -->

`Store.events` gained `with_labels`, the `with_subject` mirror (entry 257): per event,
`labelled`. Only `/api/events` opts in, so `cats_overview` and `door_stats` — which
pages this feed 8x for counts alone — pay nothing for a key they never read.
A BOOLEAN via `EXISTS`, not a count: every consumer reduces it to yes-or-no, and this
stops at a span's first live row instead of walking every labelled frame in it.

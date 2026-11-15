# Admin Identify names the loaded span it enqueues

<!-- entry 270 -->

Admin's Identify NAMES the span it enqueues ("over the N loaded visits"), because paging
silently narrowed that backfill from ~500 visits to one page of 100. The scope follows the
loaded set by design — "Show older" widens it — but nothing said so at the point of the click.

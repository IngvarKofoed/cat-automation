# Corruption page uses a start-and-timespan picker with minute paging

<!-- entry 98 -->

Corruption page picker + paging reworked: a `datetime-local` START (to the minute) + a preset TIMESPAN
(15 min … 24 h) set the window; the grid then pages by MINUTES (a 1/2/5 min-per-page selector between
Prev/Next), each page a resolved time sub-window so the header counts stay range-wide while you scan
chronologically. Feed is OLDEST-first (`corruption_feed` ORDER BY `id ASC` + forward keyset). Buckets
picker unchanged.

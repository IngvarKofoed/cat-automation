# Widest window read once and folded into every shorter window

<!-- entry 342 -->

The widest window is read ONCE and each visit folded into every window its
`start_ts` falls inside, so the two columns cannot disagree at their shared
boundary. `events` is walked in KEYSET pages under a `_MAX_STATS_PAGES` (8) budget
rather than widening `_MAX_EVENTS`: that cap bounds a FEED RESPONSE, while a busy
day exceeds 500 visits and this caller returns only counts.

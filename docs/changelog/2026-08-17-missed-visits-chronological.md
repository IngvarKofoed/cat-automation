# Missed-visit records are chronological, not worst-first

<!-- entry 166 -->

Missed-visit records are CHRONOLOGICAL, not `visits()`' worst-first. One column's panel is open
at a time, so a stable time order makes toggling Live gate → Candidate a visual diff: rows hold
their place and a recovered visit simply disappears. Worst-first reshuffles every row per column
and defeats the comparison the page exists for. The list is also uncapped — a reader must never
wonder whether they are looking at a sample.

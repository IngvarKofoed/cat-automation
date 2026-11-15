# Confirm's cat_id is a concurrency check, 409 on disagreement

<!-- entry 480 -->

`cat_id` in the body is a CONCURRENCY CHECK, not an instruction — the server
re-resolves and 409s when the two disagree. The threshold is applied at read time and
restates all history when changed (entry 425), so the name a phone displays can
genuinely differ from the name the server would give the same span a moment later.
"Yes" has to mean yes to what was on screen.

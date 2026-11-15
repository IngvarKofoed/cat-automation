# Motion tuning gained a Lighting card with histogram

<!-- entry 181 -->

Motion tuning gained a Lighting card (day-scoped sweep, a coarse histogram to pick the
cutoff from, the cutoff field) plus LIGHT calendar/day coverage; Frame review shows a
per-frame chip, dimmed when the label is assumed rather than measured.
Scorecards gained an All/Day/Night selector that scopes the SCORING — the re-run still
walks every frame, because MOG2's rolling background cannot survive a filtered input.
Day + night is an exact partition of All: frames bucket by their own timestamp, visits
whole by their first present frame (reusing the existing split), so neither drifts.
Day/Night disable without a location rather than scoring nothing; the selector resets
to All on every load, since a sticky scoring filter invites misreading a scorecard later.

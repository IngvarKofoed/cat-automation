# Scorecards can show the visits they missed

<!-- entry 165 -->

Each admin-next scorecard can now SHOW the visits it missed, not just count them:
`gate_scorecard(missed_visits=True)` returns per-visit records for the wholly-missed spans it
already clusters, so `len(missed_visits)` IS `visits.wholly_missed` and the two cannot drift.
Deliberately not `Store.visits`, which judges caught against the LIVE gate only and ignores
warm-up + `oracle_floor` — under a Baseline/Candidate heading it would list the wrong column's
misses. `interesting` rows grew `f.id`/`o.score`; the counting paths now read them positionally.
Spec: docs/specs/2026-07-27-tuning-missed-visits.md.

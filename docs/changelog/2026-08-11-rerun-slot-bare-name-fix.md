# Baseline and candidate re-runs send the bare slot name

<!-- entry 137 -->

Fixed admin-next Run baseline/candidate (400). The buttons sent the PREFIXED analyzer name (`mog2:candidate`) as the
`/api/tuning/rerun` `slot`, which `MogAnalyzer` rejects — that field is the BARE slot (`candidate`) and the analyzer adds
the `mog2:` prefix itself. Now sends the bare slot, matching the old `/admin`. Pre-existing since the Wave 2 scaffold, so
MOG2 re-runs never worked from admin-next until now.

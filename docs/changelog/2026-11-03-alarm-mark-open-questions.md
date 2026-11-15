# Sounding can show while nothing plays, left open

<!-- entry 555 -->

Left open by the review, as the spec decides them. "Sounding" can show while nothing plays:
`alarm_rev` moves only on open/close, so a Pi refusal or failure landing after the first
refetch never nudges, and an episode that played and was THEN switched off reads "Sounding
now" (rule 2; `pi_disabled` is sticky, so "still off" is unknowable). And `alarm_rev` COUNTs
all of `actions` per SSE poll per client — tens of rows a day, unindexed, never pruned.

# Flag identity is span overlap on both sides

<!-- entry 224 -->

Flag identity is span OVERLAP on both sides — the client draws the ⚑ for any flag
overlapping an event, and `add_label_flag` dedups the same way. An event's motion
cluster GROWS as later frames arrive, so an exact `(start_id, end_id)` key would mint
a second flag on a re-tap and leave un-mark with no target. So un-mark takes the SPAN
(`POST /api/label/flags/unmark`) and clears every overlapping flag.

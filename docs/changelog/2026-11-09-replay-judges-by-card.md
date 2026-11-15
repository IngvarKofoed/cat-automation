# Replay judges ours by the card and counts by visit end

<!-- entry 586 -->

The replay judges "ours" by the CARD (the whole-visit vote), not labels, and counts a visit
alarmed only if the start was decided by its `end_ts` — later starts are listed as late.
Its idle-tick skip is proven decision-identical to ticking every second. Delay is printed
over foreign visits (the spec's goal figure) and over all alarmed visits.

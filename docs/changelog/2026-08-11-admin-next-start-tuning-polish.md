# Start and Motion tuning pages polished with a coverage slots field

<!-- entry 134 -->

admin-next Start + Motion-tuning polish. Start: Store timestamp tiles span two columns and never wrap
(dd/mm-yyyy hh:mm on one line); the Location Save button + status note now share a bottom-aligned group
so the note is centred on the button. Motion tuning: the Day card's id-range readout is replaced by a
stats panel (Total frames, Events, and YOLO/Baseline/Candidate frame coverage x/y), the day `<select>`
is themed, "YOLO oracle coverage" → "YOLO coverage" (shows frames-not-yet-swept, a "Run YOLO" button,
and a new "Rerun all" checkbox → `reanalyze`), and the "present" wording is dropped for "Events".
Backend: `/api/analysis/coverage` gains an additive `slots` field (mog2:baseline/candidate coverage),
kept OUT of `oracles`/ANALYZER_NAMES so the re-run slots never leak into oracle-selection paths.

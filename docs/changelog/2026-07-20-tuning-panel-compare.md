# Tuning panel compares live, baseline and candidate scorecards

<!-- entry 25 -->

Tuning panel (vanilla JS): six param fields prefilled from edge, baseline/candidate buttons.
`/api/tuning/compare?oracle=yolo` returns scorecards for live + baseline + candidate with
per-metric deltas highlighted (green = fewer misses, red = more false triggers). Fidelity
agreement shown. Winning params for copy-paste to edge config UI.

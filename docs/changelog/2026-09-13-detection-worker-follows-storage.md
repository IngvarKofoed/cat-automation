# Detection worker coverage follows what is stored

<!-- entry 301 -->

The always-on YOLO worker's coverage now FOLLOWS what is stored instead of idling: every
frame under keep-all, motion frames under motion-only. Renamed the DETECTION worker
(operator-facing only — module, class, settings keys and `analysis.analyzer` unchanged,
per entry 147), because its verdicts feed event subjects, per-visit aggregates, the queue's
membership and the identify pass — all of which need only motion frames. Only the gate
scorecard's MISS column ever needed the non-motion half.

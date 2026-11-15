# Admin cards show per-visit detection stats and corrupted chip

<!-- entry 106 -->

Admin Activity cards now surface the entry-101 per-visit stats: a quiet mono line under each
caption shows YOLO's detection RATE over the visit's motion frames (a recall proxy) plus PEAK
and MEAN detection confidence, from `event["detection"]`. `—` marks not-measured (span unswept)
vs `0%` for a swept miss — the same honest distinction the backend records.
Also renders the `corrupted` subject chip (emitted by the backend since entry 101 but never
shown — a corrupted event drew no chip); it takes the corruption purple, so a glitch-explained
visit never reads as a caught/missed verdict. Presentation only; user dashboard unchanged.

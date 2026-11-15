# Motion tuning day picker is a four-week calendar

<!-- entry 135 -->

Motion-tuning "Day" is now a compact 4-week, Monday-first calendar (oldest week top, current week
bottom) that doubles as the day picker — each cell shows the day's events + Y/B/C sweep-coverage %;
the selected day drives the tiles (all/uncapped events, % swept on YOLO/baseline/candidate) and sweeps.
New `GET /api/tuning/calendar` + `Store.tuning_calendar` return per-LOCAL-day frame/event/coverage
aggregates, bucketed by a browser-supplied tz offset. It runs on its OWN short-lived WAL read
connection (NOT the shared write-locked one) so a big-window scan can't stall the collector —
the starvation class entries 102-105 removed. Cell keys use that SAME fixed offset as the backend,
so days across a DST boundary don't blank out (only the ~1h-near-midnight bucket edge remains).
Also: custom-styled checkboxes + removed number-input spinners; "seeded from edge (edge)" text fixed.

# Events derive a corrupted subject and per-visit detection aggregates

<!-- entry 101 -->

`events()` derives a `corrupted` subject + attaches per-visit detection aggregates — read-time, no
schema/sweep. `corrupted` (between bird/unrecognized) fires only when YOLO saw NOTHING and any frame in
the visit's id span is `corruption`-flagged, so a cat in a corrupt frame stays `cat`. `event["detection"]`
= {ratio, conf_max, conf_mean} over the visit's motion frames, RAW; ratio is null when unswept ("not
measured") vs 0.0 for a swept miss. Spec: docs/specs/2026-07-23-visit-detection-aggregates.md.

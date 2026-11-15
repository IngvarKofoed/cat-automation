# Frame review shows each frame's MOG2 blob area

<!-- entry 174 -->

Frame review shows each frame's MOG2 blob area, so the number behind a motion=1 frame can be
compared by eye against a still one — how the band thresholds get read off real data.
`GET /api/frames/sample?flags=1` now attaches `area` (the LIVE gate's `frames.area`, the same
reading as the edge UI's Area badge), free off the row it already fetched. Additive: absent
`flags` the payload is byte-identical.

# A missed visit says why the gate dropped it and which knob to turn

<!-- entry 170 -->

A missed visit now says WHY the gate dropped it and which knob to turn. Each record carries its
frames' MOG2 blob `area` bucketed into the gate's four actual reject paths — `near_zero`,
`below_min`, `above_max`, `in_band` — with the dominant one as `reason` and a `{param, direction}`
`fix` (e.g. above_max → raise max_area_fraction, the common top-down "cat fills the ROI" case).
`in_band` means the area PASSED the band and only the persistence debounce dropped it.

# min_frames is floored at 1 with no upper clamp

<!-- entry 408 -->

`min_frames` is floored at 1 with NO upper clamp, unlike `limit`: any floor above the
largest visit empties the queue legitimately, so a bound would not prevent that —
`hidden_thin` is what keeps it from being SILENT. 1 is the API's no-op default and the
default-ON behaviour lives entirely in the client, so no other caller of
`/api/label/queue` changes.

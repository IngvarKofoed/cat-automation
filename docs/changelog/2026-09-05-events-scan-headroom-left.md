# Feed still scans 200k motion frames, left deliberately

<!-- entry 258 -->

Remaining headroom on that feed, deliberately not taken: it still fetches the newest
`_EVENT_SCAN_FRAMES` (200k) motion frames and clusters them all, to keep only the newest
500. Costs ~90 ms once a store's motion tail exceeds the cap (~165 ms/call measured at
540k motion frames); an early-stop after `limit` clusters would fix it, at the price of
reworking the `truncated`/partial-oldest-cluster semantics.

# visit_label_state is the one rule behind probe and write

<!-- entry 478 -->

New `Store.visit_label_state` is the SINGLE rule behind both the probe
(`GET /api/label/visit`) and the write, so the button can never be drawn for a tap the
server then refuses. Reasons, in order: `no_crop`, `all_labelled`, `unnamed`,
`retired`, `contested`. Identity comes from `_aggregate_identity` — the same voter
`events()` uses — so a confirm cannot name a cat the feed did not show.

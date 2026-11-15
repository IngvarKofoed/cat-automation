# Edge-compute wire format lives in one shared module

<!-- entry 13 -->

Edge↔compute wire contract now lives in `shared/wire.py` — single source of truth.
Edge serializes frames through it, compute ingest (`EdgeClient`) parses through it; format can't drift.
Round-trip test locks it. One wire change: `/stream` emits `X-Area` on every part (matching `/status`),
not just when motion. `/status` is the camera health and liveness oracle; stream is the data plane.

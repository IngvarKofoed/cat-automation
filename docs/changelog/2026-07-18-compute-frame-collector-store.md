# Compute stores every edge frame in a bounded indexed store

<!-- entry 16 -->

Compute-side always-on frame collector: saves every edge frame (motion + non-motion) with motion flag + area
to a bounded (default 5 GB) SQLite-indexed store, indexed by recv_ts. FastAPI browse UI (port 8001) shows
frames in time order with motion frames visually marked; triage presets (Missed? = non-motion by area;
False triggers = motion by area) make motion-gate tuning findable. Reuses EdgeClient, writes raw JPEG bytes
(no re-encode) — purpose is *seeing* where the edge motion gate is wrong (missed cats + false triggers).

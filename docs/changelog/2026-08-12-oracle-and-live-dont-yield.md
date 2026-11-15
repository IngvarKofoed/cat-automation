# Oracle and live-naming loops yield only to manual jobs

<!-- entry 139 -->

The two always-on YOLO loops (oracle + live-naming) deliberately do NOT yield to each other — only to manual jobs.
Safe because a same-frame detect is idempotent (`analysis` is `INSERT OR REPLACE` on `(frame_id, analyzer)`, all writes
serialized on the store lock), so an overlap wastes a little GPU and never corrupts. Feeding the oracle's `running` into
live-identify's `is_busy` would have been WRONG, not just conservative: `running` is an INTENT flag (true whenever the
toggle is on), so it would have suppressed naming for as long as the oracle was enabled.

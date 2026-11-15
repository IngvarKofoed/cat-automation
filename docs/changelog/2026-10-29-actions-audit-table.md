# New actions table is a durable audit log of alarm episodes

<!-- entry 528 -->

New `actions` table — ARCHITECTURE's Action entity, real for sound: one row per episode
or test, kept through `clear()` and eviction as an audit log. The deciding frame is the
(`frame_id`, `recv_ts`) PAIR, so a reused id reads "gone" (entries 490/504). Rows a restart
left open close as `interrupted` at launch — live app only, so a CLI tool can't close a
running server's episode. `visit_start_ts` makes the start latency readable per row.

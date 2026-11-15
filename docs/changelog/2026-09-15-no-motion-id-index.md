# Rejected a (motion, id) index for eviction ordering

<!-- entry 310 -->

Chose that over adding a `(motion, id)` index — also fast — because an index costs write
time on every insert forever to speed a maintenance path, and this one already exists.
The trade: victims are an exact id-prefix only while `recv_ts` is non-decreasing with `id`
(entry 278: assumed, not enforced), and if it ever stepped back the `max()`-tracked marker
OVER-claims unmeasurability rather than under-claiming — the fail-safe direction.
Victim lists verified byte-identical over 3000 rows.

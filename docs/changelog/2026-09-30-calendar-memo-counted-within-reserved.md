# Day memoized only if counted ids lie inside reserved ids

<!-- entry 387 -->

Review repairs. A day is memoized only if the ids it actually COUNTED lie inside the
ids the placeholder RESERVED. The reservation comes from a `recv_ts` seek, so the two
coincide only while `recv_ts` is non-decreasing with `id` — assumed, never enforced
(entry 278) — and where it isn't, a mutation to the difference slips past the
placeholder and a wrong count is cached with nothing left to correct it.

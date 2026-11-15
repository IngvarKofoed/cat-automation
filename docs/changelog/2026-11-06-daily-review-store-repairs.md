# Recording probe plan pinned; local_date is the one day rule

<!-- entry 568 -->

Review repairs, store side. The hourly recording probe is a named constant whose plan — a seek
on `idx_frames_recv_ts` — is pinned by a test (verified to fail against a de-indexed `+recv_ts`):
~2,200 run per request, uncached, and the result is identical either way. `Store.local_date`
is now THE day rule, used by both the day buckets and the API cache key.

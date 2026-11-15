# Known limits of the paged feed

<!-- entry 271 -->

Known limits of the paged feed, deliberately left. An empty page carrying `truncated:true`
(reachable when a scan-capped window's sole cluster is popped) stops paging: trusting the
flag instead would re-request the same `until_id` forever, since no new events move the
keyset — a real fix needs the backend to expose the scanned window's floor.
Infinite scroll also turns entry 258's fixed floor into one 200k-frame scan per page, on
the shared write connection; a short-lived WAL read connection (as `tuning_calendar` and
`labeled_visits` use) is the fix if it bites. And user-page paging exists only where
`IntersectionObserver` does, with no button fallback.

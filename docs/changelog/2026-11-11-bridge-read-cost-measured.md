# Bridge boundary-box read is a primary-key probe, measured

<!-- entry 597 -->

Measured on a 2M-frame replica with no ANALYZE: the boundary-box read is a primary-key probe
(plan pinned by a test), 7 ms lock-held per 200k-frame scan window; a feed page went 87 →
114-133 ms, mostly JSON parsing outside the lock. A whole bridged walk is FASTER (2.9 s vs
5.9 s): fewer visits to annotate. Headroom left: bridge newest-first and stop at `limit`.

# Tests pin the decode-ahead contract

<!-- entry 400 -->

Tests pin the decode-ahead's whole contract: order preserved at five (workers,
lookahead) settings, byte-identical verdicts vs a forced-serial run, an undecodable
frame skipped in place (its exception now surfaces at `result()`, not inline), and a
cancel persisting exactly the prefix it computed. Verified to FAIL against an injected
reordering — the `(1,1)` serial case correctly still passes, a one-element deque
making `pop` and `popleft` the same.

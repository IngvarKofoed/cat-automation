# Paging stops at the window floor without setting truncated

<!-- entry 350 -->

Paging stops when the keyset bound `(oldest start_id − 1)` falls below the window's
own `since_id`, WITHOUT setting `truncated`. That bound crosses the floor whenever
the oldest cluster starts at the window's first frame; continuing asked for an
inverted range, got nothing, and reported truncation — putting a "figures are
incomplete" banner on a complete reading.

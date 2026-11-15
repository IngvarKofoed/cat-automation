# Live naming auto-starts at launch when a model is promoted

<!-- entry 100 -->

Compute auto-starts live naming at launch when a model has been promoted (an active gallery exists),
not only when the operator left the intent on — a promoted model means new visits should be named
without a manual toggle. First-ever enable still seeds the watermark to the frame horizon (names only
NEW visits; back-identifying history stays the manual Identify pass). No active model → worker idles (no-op).

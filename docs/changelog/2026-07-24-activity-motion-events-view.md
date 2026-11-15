# User-facing activity view lists motion-based events

<!-- entry 44 -->

Added `#activity` — a user-facing SPA view showing motion-based events (time-gap frame clusters).
New Store.events() + /api/events reuse _gap_split/_VISIT_GAP_MS to prevent clustering drift.
No oracle required — populated instantly. Event thumbnail is the peak-area frame; click opens
an in-view player (play/pause/scrub, ~8 fps) via /api/frames + filmstrip. From/to date filter; cat-id filtering deferred.

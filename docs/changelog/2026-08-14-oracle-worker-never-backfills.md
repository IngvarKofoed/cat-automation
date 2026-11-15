# Oracle worker never backfills, seeding to the horizon on every start

<!-- entry 149 -->

The always-on YOLO-oracle worker NEVER backfills: every `start` — an operator switch-on and the
launch-time `restore` — seeds the watermark to the current frame horizon, superseding entry 138/142's
first-enable-only seed. A re-enable no longer drains the frames captured while it was off (an
unbounded GPU hold that delays coverage of *today*, the thing the worker exists to keep current).
Accepted cost: a restart forgets an un-drained tail (e.g. one left by a long manual job) — those
frames need a manual sweep. Catch-up within a running worker is unchanged.

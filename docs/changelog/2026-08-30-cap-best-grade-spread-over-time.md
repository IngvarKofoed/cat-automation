# Cap picks best grade first and spreads over time

<!-- entry 232 -->

`cap_per_cat` picks best-grade-first (gallery → ok → poor → ungraded) and, where a tier
overflows the budget, samples it EVENLY OVER TIME rather than from the front: a
contiguous run of crops is one visit in one light, the least useful thing to fill a
gallery with. Time order is `src_frame_id` order (ids are assigned on receive), so no
extra column is needed. It discards no labels — a later build may enrol different crops
— and can never drop a cat, which is why it runs before the cold-start guard.

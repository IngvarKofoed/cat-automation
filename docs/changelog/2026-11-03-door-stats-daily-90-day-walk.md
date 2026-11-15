# door-stats/daily answers 90 days for every cat from one walk

<!-- entry 557 -->

`GET /api/door-stats/daily` always answers 90 days for EVERY cat, from one keyset walk of the
same `events()` feed Visits counts (`with_subject=False`), so the chart can never name a visit
differently from Visits, Cats or Activity. The window is a client-side slice: switching is a
pure re-render, and a day reads the same in every window.

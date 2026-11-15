# loadRuns gained a sequence guard

<!-- entry 366 -->

`loadRuns` gained a sequence guard now that it drives the cats table as well as its
own; two overlapping fetches landing out of order would paint an older run's recall
under the newer stamp. A grade tick re-renders WITHOUT refetching it (verified: the
runs request count is unchanged across a tick) — the ticks change no run, only the
client-side comparability verdict, and refetching per checkbox would walk back
entry 201.

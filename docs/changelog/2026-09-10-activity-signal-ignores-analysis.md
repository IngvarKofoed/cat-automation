# activity_signal deliberately ignores analysis

<!-- entry 285 -->

`activity_signal` deliberately does NOT learn about `analysis`, so a visit analysed to a
NO-DETECTION result pushes nothing over SSE and updates on the next reload. Adding a
verdict counter would nudge every connected client every tick — the always-on oracle
writes continuously.

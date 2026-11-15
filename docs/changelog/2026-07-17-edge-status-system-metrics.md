# Edge /status reports host CPU and memory

<!-- entry 12 -->

Edge reports host CPU% and memory on `GET /status` under a `system` object
(`cpu_percent`, `mem_percent`, `mem_used_mb`, `mem_total_mb`), shown as two badges
in a slim top bar in the config UI. Measured with `psutil` — one portable path for Pi OS
and macOS. CPU% is host-wide (not per-process) and resampled at most once per ~2s.
Fails soft: psutil missing or a read error → `system: null` and /status still 200.

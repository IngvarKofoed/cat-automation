# Edge night-light scheduler runs from sun times offline

<!-- entry 118 -->

Night-light scheduler (edge-side): drives GPIO channel LOW from sunset−30min to sunrise+30min to power
a lamp; sun times computed offline via `astral` (no network). Survives compute-PC outages and
preserves manual switch flips (write-on-change). Configured in the config UI (channel, offsets,
location); no-ops off a Pi. Edge-side because fixed camera illumination must survive an outage,
diverging from the deferred intent-based Control API (lock/sound/deterrent-light).

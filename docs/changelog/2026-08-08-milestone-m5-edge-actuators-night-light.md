# Milestone M5 — Edge actuators + night light

<!-- milestone M5: entries 114–120 -->

**M5 — Edge actuators + night light** (114–120). Three GPIO relay channels (BCM 26/21/20) expose raw
HIGH/LOW level, not on/off — the board is active-low, so they boot HIGH (released). An autonomous
astronomical night-light schedule drives a channel from sunset to sunrise with configurable offsets,
computed offline via `astral`. Edge-side deliberately: fixed camera illumination must survive a
compute-PC outage, unlike the deferred intent-based Control API.

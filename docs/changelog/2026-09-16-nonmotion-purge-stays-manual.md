# Retroactive non-motion purge stays manual by decision

<!-- entry 313 -->

The retroactive non-motion purge stays MANUAL by decision. Outside `tuning` no non-motion
frames are being stored, so every frame that job removes comes from a previous tuning
window — it is retroactive by construction, and auto-running it on a stage change would
make a mode toggle irreversibly destructive. The Start page instead links to it whenever
leftover non-motion frames exist, keyed on `count − motion_count` so the hint appears from
state and disappears once there is nothing to reclaim.

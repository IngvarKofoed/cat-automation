# Alarm start votes over whole presence with a 1 s hold

<!-- entry 578 -->

The foreign-cat alarm's START rule now votes over the cat's whole PRESENCE and must stay
foreign 1 s, replacing the 5 s sliding window that gave a long resident visit a fresh draw
every second (one lookalike-led window set it off). A presence is the run of counted frames
with no gap over 5 s, reaching back at most 2 minutes. Stop rule, switches, clip loop and
processing step are unchanged. Spec: docs/specs/2026-11-05-alarm-presence-vote.md.

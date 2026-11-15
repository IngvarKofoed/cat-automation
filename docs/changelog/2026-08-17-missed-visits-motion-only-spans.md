# Missed-visit panel carries motion-only spans

<!-- entry 167 -->

`/api/tuning/compare?missed=1` carries `motion_only_spans` alongside the records, and the panel
banners them even when the list is EMPTY. Across a motion-only or purge span the non-motion
frames a miss lives in were never stored, so a short list there is an absence of evidence, not
good recall — the "an empty danger set reads as safe" trap of entries 97/126, whose sharpest
instance is a page whose whole job is showing what was missed.

# NoIR swap prerequisites recorded before motion re-tune

<!-- entry 179 -->

`docs/NOIR_SWAP.md` records what must happen before the NoIR module's motion re-tune.
Load-bearing item: AWB is never touched today, and its per-frame R/B gains move the
GRAY MOG2 runs on — so tuning before locking it calibrates against a sliding baseline.
NoIR is the worst case (no IR-cut: daylight NIR cast varies hourly; night has no colour
to estimate from). Also: only ONE param set exists, so day/night can't diverge yet;
night may beat day (co-axial IR hides shadows); `motion_downscale` is a noise knob.

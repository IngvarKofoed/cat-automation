# Edge can lock white balance and select a tuning file

<!-- entry 185 -->

Edge can LOCK white balance and select a libcamera tuning file — the NoIR swap's
prerequisite (docs/NOIR_SWAP.md item 1). New `awb_gains` (null = auto, [r,b] = locked)
and `tuning_file` settings, `POST /api/awb/lock` (settle ~10 frames, lock what AWB
converged on, persist), and `awb` in `GET /api/capabilities`. Modelled on `focus`.
Why it matters beyond colour accuracy: auto WB re-estimates every frame, so a STATIC
scene's pixels drift — moving the day/night colourfulness statistic and smearing the
day colour cue. On a NoIR sensor daylight's NIR component makes that drift hourly.
Tuning file is applied BEFORE gains everywhere (boot, POST): it rebuilds the camera,
so gains pushed first would be discarded — and it is re-pushed after, so a reopen
never silently reverts a lock to auto. A bad tuning name falls back + logs, never
bricking capture. Both re-applied on a self-heal reopen.

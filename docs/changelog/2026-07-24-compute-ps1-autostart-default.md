# compute.ps1 defaults collector autostart on

<!-- entry 45 -->

`compute.ps1` now defaults `CAT_COLLECT_AUTOSTART=1` (a caller-set value still wins; =0 launches
stopped). The Windows box is the dedicated collection PC, so a stop/start to `git pull` resumes
collecting with nothing to remember — the real footgun, since the one-click Resume prompt (31)
was being forgotten. Per-machine and explicit, not inferred: `compute.sh` (dev) stays off, so
changelog 28's "a bare launch never silently writes" holds where it matters.

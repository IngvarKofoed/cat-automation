# compute.ps1 exports a 1 TiB store cap, not the 5 GiB default

<!-- entry 19 -->

`compute.ps1` now sets and exports `CAT_COLLECT_MAX_BYTES`, defaulting the frame-store
retention cap to 1 TiB (the Windows PC has ample disk) vs. the app's 5 GiB default.
Prior scripts only echoed the cap without exporting it, so the app silently used 5 GiB;
a caller-set env var still wins.

# Bucket density decimates by wall-clock time, not frame index

<!-- entry 34 -->

Buckets viewer refinements. The clock end bound is now inclusive through the selected 3 h block
(+step), so the newest frames — which fell after the last 21:00 option — are reachable, not silently
excluded. Added a Clear-window button (reset the grid to re-see the saved-buckets list), a total
frame count on the "All frames" badge, and a "Per hour" decimation density alongside "Per minute".
"Per minute/hour" now decimates by TIME (one frame per recv_ts interval, via /api/frames/sample?per_ms)
instead of by frame index — so the rate is a true wall-clock rate regardless of capture fps, clock-window
width, or collector gaps. The prior index-stride computed its count from the (often huge, mostly-empty)
clock window, yielding near-every-frame at "1/min".

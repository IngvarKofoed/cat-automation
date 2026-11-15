# Enqueue shows in-flight state and a live ETA

<!-- entry 39 -->

Motion view: enqueuing an oracle now shows an in-flight state ("Enqueuing YOLO…", buttons
disabled) and the running job shows a live ETA ("~Xm Ys left"). The first YOLO/BSUV enqueue
blocks several seconds on the synchronous ensure_available() dep import (torch/ultralytics),
during which the job isn't queued yet — so the click looked dead. ETA is client-side: rate
extrapolated from progress across polls, re-anchored per job (total is in the anchor key, so
it re-anchors when the denominator resolves), dropped when idle. No server change.

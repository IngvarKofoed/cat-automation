# One shared frame-enlarge modal across tuning and frame review

<!-- entry 169 -->

Clicking a frame in a missed-visit filmstrip enlarges it, and the enlarge modal is now ONE
module-level `openFrameModal` shared with Frame review rather than a second copy — the pages
differ only in the meta line they pass (Frame review's adds identity + the motion/corrupt
markers; a tuning strip has just the detection). Escape is one app-level listener now that the
modal outlives any single view; the Activity player keeps its own, since it also stops playback.

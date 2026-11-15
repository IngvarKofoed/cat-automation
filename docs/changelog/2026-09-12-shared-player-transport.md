# Player transport is shared with Activity

<!-- entry 293 -->

The player transport is now module-level and shared with Activity, which supplies its
own stage (one full frame + box) and stats as before — so play/pause, scrub, position,
Escape and backdrop-close can't drift between the two. Each cell is FIXED size —
the frame cell takes the frame's own aspect ratio — since a crop's box changes shape
frame to frame and content-sized cells would resize 5× a second.

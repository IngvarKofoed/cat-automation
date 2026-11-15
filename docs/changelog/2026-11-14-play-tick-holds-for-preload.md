# Play tick holds until the next frame is loaded

<!-- entry 608 -->

The Play tick HOLDS until the next frame's preloaded `Image` is `complete` (loaded or failed):
a slow link stutters instead of showing an unloaded frame. Only the tick waits — scrub, tile
tap and arrows still jump straight to their frame. Rejected: every control waiting, which
needs a requested-vs-shown split. No bound on a hold, by decision; the reader can scrub past.

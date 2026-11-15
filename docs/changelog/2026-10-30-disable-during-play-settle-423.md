# Disable inside play's settle window answers 423

<!-- entry 535 -->

Review repair, edge: a disable landing inside `play()`'s 0.3 s settle window no longer
answers 200 for the clip it just cut. The compute would have counted it played and timed
the next clip off a sound that never finished. Now 423, like any play while disabled.

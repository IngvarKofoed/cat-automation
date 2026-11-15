# First-model gap where nothing detected is fixed

<!-- entry 300 -->

Fixes the hole that motivated it: between switching to motion-only capture and promoting
a first gallery, NOTHING detected anything. The detection worker bailed under motion-only
capture and live-identify bails without an active model, so visits landed `unanalyzed`
and the annotation queue stayed empty — noticed only when the queue you came to work was
blank.

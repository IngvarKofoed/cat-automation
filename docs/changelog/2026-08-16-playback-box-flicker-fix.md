# Activity playback no longer flickers its detection box

<!-- entry 160 -->

admin-next Activity playback no longer flickers its detection box. It hid the box and re-drew it on
every frame's load — a hide/show cycle 5× a second. Natural dimensions are now learned ONCE and
cached (every stored frame shares the camera's post-transform size), so the box is drawn
synchronously per frame, as the user dashboard already did. Measured: 2 visibility transitions across
~15 played frames (the one genuinely box-less frame) instead of one pair per frame.
This also makes a stale box structurally impossible — the box is drawn from the frame being shown,
not from a closure the previous frame left on `onload` (entry 156's fix, now unnecessary).

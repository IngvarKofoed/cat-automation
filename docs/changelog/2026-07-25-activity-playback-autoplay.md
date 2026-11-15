# Activity playback autoplays at 5 fps

<!-- entry 48 -->

Activity playback now auto-plays: opening an event (and each Prev/Next hop, both routed
through openEvent) starts from frame 0 instead of waiting for a Play click. Playback rate
dropped 8→5 fps to match the ~5 fps capture, so the clip plays back at real speed.

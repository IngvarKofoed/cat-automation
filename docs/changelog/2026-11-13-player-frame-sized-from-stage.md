# Player frame is sized from the stage, not the picture

<!-- entry 606 -->

The user player's frame is sized from the STAGE, never from the picture: `.stage` is a size
container and the frame is the largest `--frame-r` rectangle inside it (the first container
query in either dashboard). Sized from the picture, it collapsed to the alt text (103×66)
whenever no picture was present — on open, on a hop, and on WebKit for every frame playback
reached early. Spec: docs/specs/2026-11-14-user-player-frame-size.md.

# Edge sheds stalled streams and exits on a wedged grabber

<!-- entry 94 -->

Edge now survives a camera-grab stall instead of leaking itself to death. A wedged CSI read froze
`frame_id`, and each compute `/stream` reconnect then leaked a Werkzeug handler+FD until the process died.
Now `/stream` sheds its handler after `CAT_EDGE_STREAM_STALL_S` (15s) idle, and a new watchdog `os._exit`s
a frozen grabber (70=wedge, 71=never-got-a-frame) for systemd to respawn (new `deploy/cat-edge.service`,
`Restart=always`); fires only while `Grabber.is_running()`. Spec: docs/specs/2026-07-23-edge-grab-stall-recovery.md.

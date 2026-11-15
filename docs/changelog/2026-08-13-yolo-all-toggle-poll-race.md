# YOLO all toggle no longer fights its own poll

<!-- entry 146 -->

Start-page "YOLO all" toggle no longer fights its own 3s poll: an `oracleSubmitting`
in-flight flag makes `renderStats` skip the checkbox while a start/stop POST is pending.
The handler's `disabled = true` guard was being unlocked by the very next poll (which
re-derives `disabled`/`checked` from stale server state), so the double-click race it
exists to prevent was still reachable, with a visible checkbox flicker.

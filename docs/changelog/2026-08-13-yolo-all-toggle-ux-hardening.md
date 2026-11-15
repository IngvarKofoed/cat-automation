# YOLO all toggle keeps error history and can always switch off

<!-- entry 145 -->

Start-page "YOLO all" UX hardening: a sticky `last_error` is appended as history rather than replacing the state
line (branching on it first hid "Idle"/"Sweeping" for the rest of the process's life after one transient fault);
the toggle can always be switched OFF, only ON is blocked in motion-only capture (a control you can't switch off
is a trap); and the disabled checkbox is dimmed once, not twice — stacked opacities rendered it invisible.

# CleanupManager.start_recut gained an end-to-end test

<!-- entry 458 -->

`CleanupManager.start_recut` gained the end-to-end test its two sibling kinds already
had. Every other recut test drove `recut_crops` directly or was blocked by a busy
fake before `start_recut` was reached, so nothing checked the manager's own wiring —
`recut_plan`'s target staying in lockstep with the one handed to `recut`, and the
progress closure's cancel contract. Targets `letterbox` so the move is a copy, keeping
the test cv2-free like the rest of that file.

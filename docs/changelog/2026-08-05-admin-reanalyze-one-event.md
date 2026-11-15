# Admin playback can re-analyze a single event

<!-- entry 107 -->

Admin playback modal gained a per-event "Re-analyze frames" button: re-detects just the OPEN
event's motion frames with yolo-serial (reanalyze + motion_only, scoped to its [start_id, end_id]),
so one visit whose subject looks wrong is re-detected without re-sweeping the whole loaded block.
Shares the yolo-serial queue + running state with the whole-events Analyze button (both disable
while a serial sweep runs); feedback lands in the modal, progress on Sweeps.

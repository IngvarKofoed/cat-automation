# Live-populated windows have non-uniform detection coverage

<!-- entry 76 -->

Known limit of the live worker: it writes `yolo-serial` verdicts only within visit spans, so
gate-scorecard / annotation coverage over a live-populated window is non-uniform — tune the motion
gate from a full manual sweep, not a window the live worker has already touched.

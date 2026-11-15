# Two-cats check removed after measuring duplicate head detections

<!-- entry 498 -->

The two-cats check is GONE, not tuned a third time. Measured on the live store: it
blocked 3 of the 8 newest visits, and the frame behind one of them holds a single cat
with only its head in shot — `yolo-serial` put two boxes ≥0.3 on that one head. So the
box count measured duplicate detections, exactly as the vote version (479/493)
measured embedding noise.

# Second finger disarms swipe; no touch-action on stage

<!-- entry 252 -->

Two rails the swipe needs. A second finger DISARMS the gesture: merely ignoring its
`touchstart` left the first armed, so a pinch ended with a large `dx` and hopped visits.
And there is deliberately NO `touch-action` on the stage — `pan-y` would kill pinch-zoom on
the one image worth zooming; the axis latch calls `preventDefault` only once a drag has
committed to horizontal, which claims the axis without taking the browser's own gestures.

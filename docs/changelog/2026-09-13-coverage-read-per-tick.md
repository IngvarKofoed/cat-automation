# Coverage mode read per tick, workers restore unconditionally

<!-- entry 302 -->

Coverage mode is read per TICK, never applied via stop/start, and both workers now restore
unconditionally on a live app. Load-bearing: every `start` re-seeds the watermark to the
frame horizon (entries 149/150), so driving a stage change through a restart would
silently discard the un-drained tail. Operator-visible on the first restart: anyone who
had "YOLO all" off finds it running — the coverage tuning wanted anyway, but a real
GPU-load change.

# Diagnostic root-causes batched vs serial YOLO disagreement

<!-- entry 56 -->

Added `compute/tools/diff_yolo_batch_serial.py`, a read-only diagnostic root-causing a `yolo`
vs `yolo-serial` oracle disagreement: coverage parity, verdict diff, visit reconstruction, and
`--rerun` re-running both YOLO paths on disagreeing frames. Key insight it encodes: `gate_scorecard`
scores each oracle over only ITS OWN analyzed frames, so unequal coverage or cross-session
`CAT_YOLO_*` drift (invisible — `detail` omits imgsz/conf) can move a matrix column ~15pt, no bug.

# Labelled read's query plan is pinned by a test

<!-- entry 506 -->

The new read's plan is pinned by a test: `d` outer via covering `idx_dataset_src`, `f`
via covering `idx_frames_recv_ts`, nothing scanned. Measured with no `ANALYZE` (266),
and it corrects the assumption that motivated the pin — driving from `frames` is NOT a
full scan: SQLite transfers the range through the `f.id = d.src_frame_id` equality, so
it walks the span's FRAMES rather than its few labels. Identical result either way,
which is how entries 229/265/276/307/385/391's class keeps recurring.

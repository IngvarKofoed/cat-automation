# Labelled read joins frames on the id and recv_ts pair

<!-- entry 504 -->

That read JOINs `frames` on the (`src_frame_id`, `src_recv_ts`) PAIR. On the id alone a
stale pre-`clear()` row marks a brand-new visit as decided and the nav skips a visit
nobody has ever looked at (entry 490). It also drops a label whose source frame has
EVICTED, diverging from `visit_label_state`, which keeps those: the honest direction
here is a handled visit re-appearing, never one silently skipped.

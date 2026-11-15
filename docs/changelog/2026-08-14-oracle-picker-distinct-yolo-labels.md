# Oracle picker labels serial and batched YOLO distinctly

<!-- entry 148 -->

The oracle picker labels the two YOLO personas distinctly — "YOLO" (serial, the trusted
oracle) vs "YOLO (batched)". Dropping the slug had briefly made both read "YOLO", which
is worse than the slug: the picker chooses what a visit-recall scorecard is scored
against, and the batched persona over-detects (entry 54), so an indistinguishable pair
invites reading a scorecard against the wrong oracle.

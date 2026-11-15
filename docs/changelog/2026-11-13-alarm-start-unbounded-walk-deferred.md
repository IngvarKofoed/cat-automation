# Unbounded visit walk runs only when the floored read hit its floor

<!-- entry 604 -->

An alarm's start runs the UNBOUNDED visit walk only when the floored once-a-second read stopped
at its floor (a visit over 2 min, or the previous alarm's end); otherwise that read already is
the exact start. The walk sat before the row write and the first clip, and a long bridged nap
makes it long — the deterrent must not wait on a bookkeeping field.

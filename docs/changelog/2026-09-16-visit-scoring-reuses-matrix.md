# Visit scoring reuses the existing embed and distance matrix

<!-- entry 317 -->

It rides the SAME embed and the SAME N×N distance matrix `run_feasibility` already
builds — held-out scoring is that matrix with a visit's own columns masked — so the
honest number costs numpy, not GPU. Additive: absent `visit_groups` the result dict
is byte-identical, so nothing that consumed the old shape moved.

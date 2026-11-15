# Validation-runs table gained a Visits column

<!-- entry 355 -->

The validation-runs table gained a **Visits** column — the held-out visits actually
scored, which is the sample the accuracy is measured over. Crops was the only count
on the row and is the wrong x-axis for "did more labelling buy anything": visit-held-out
scoring hides a visit WHOLE, so crops added inside visits already present move nothing.
Read off the stored `metrics.visits`; frontend only.

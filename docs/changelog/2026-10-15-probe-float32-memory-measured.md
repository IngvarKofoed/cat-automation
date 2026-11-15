# Float32 probe cut peak memory about 4.5x with identical metrics

<!-- entry 462 -->

Measured on synthetic 768-d embeddings with the visit block on, peak RSS: n=3996
1335 -> 465 MB, n=7998 5111 -> 1305 MB — a 4.5x smaller n² coefficient, with `auc`,
`suggested_threshold` and kNN accuracy identical to 1e-6 (the float32 change). Do NOT
read peak RSS above ~3 GB on the dev Mac: at n=12000 the old code ran 36 s against
14 s and reported LESS memory than the fit predicts, because macOS compressed pages.

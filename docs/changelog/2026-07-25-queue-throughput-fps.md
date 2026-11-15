# Analysis queue running line shows fps beside the ETA

<!-- entry 52 -->

Analysis-queue running line now shows throughput as `N.N fps` alongside the ETA. The rate
(frames/sec, client-side average since the job's anchor poll) was already computed to derive
the ETA — it's now surfaced instead of only its reciprocal. Presentation only.

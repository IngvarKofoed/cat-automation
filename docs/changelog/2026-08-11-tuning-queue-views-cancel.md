# Tuning cards gained per-type job queues with per-row cancel

<!-- entry 136 -->

Motion-tuning cards gained per-type job queues + per-row Cancel (spec: docs/specs/2026-07-26-tuning-queue-views.md).
Each AnalysisManager job now carries a stable `job_id` and UI descriptors — `category` ("mog2" vs "coverage"),
`since_ts`, and a frame-count `total` estimate — all computed at enqueue OFF the manager lock; `/api/analysis/status`
reports them on the running + each queued job. New `cancel(job_id)` + `POST /api/analysis/cancel/{job_id}` cancels the
running job OR drops one specific pending job (serial-drain + FIFO invariant intact). Frontend: a shared, manager-agnostic
`renderQueue` table (Status · Name · % · x/y frames · FPS · Time to complete · Cancel) per card, filtered by category —
coverage runs under "YOLO coverage", MOG2 reruns under "MOG2 candidate params", both VIEWS of the one serial FIFO (never
parallel). Only the running row shows FPS + a finish ETA (ported `/admin` etaAnchor); queued rows show neither, so no
borrowed cross-type rate is ever displayed. `count_in_range` gained a `motion_only` filter for the reanalyze estimate.

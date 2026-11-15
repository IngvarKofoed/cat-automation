# Training page runs the feasibility probe as a background job

<!-- entry 61 -->

Training page (`#train`) — the learning loop's Train stage in the compute UI; only "Validate" is built.
Validate runs the DINOv2 feasibility probe as a cancelable background job on a new dedicated
`TrainingManager` (own queue, separate from the sweep `AnalysisManager`), with a gallery/ok/poor A/B and the
report in-page; runs persist to a durable `feasibility_runs` table. The probe pipeline moved to
`compute/identification/probe.py` (CLI now a thin wrapper). Build/promote deferred. Spec: docs/specs/2026-07-16-training-page.md.

"""The learning loop's Train stage — collection, annotation, training + promotion.

This package is the compute-tier home the architecture reserves for the
human-in-the-loop teaching loop (Collect → Annotate → Train → Run). Its first
inhabitant is ``runner.TrainingManager``, the dedicated background job queue that
drives the Phase-1 feasibility probe (and, later, gallery-build and promote) off
the dashboard's ``#train`` page — a sibling of the oracle-sweep
``compute/analysis/runner.AnalysisManager``, deliberately a *separate* queue so
training and sweeps never share a dedup namespace or contend for one slot.
"""

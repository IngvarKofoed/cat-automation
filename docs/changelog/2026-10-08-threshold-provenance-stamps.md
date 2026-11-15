# Threshold writes carry built-value and source-run stamps, bounded 0 to 2

<!-- entry 426 -->

Two provenance stamps ride that write because both outlive the run that justified them:
`metrics.threshold_built` (copied from the column only when ABSENT — no existing row has
it, so a first override would otherwise destroy the built value) and
`threshold_source_run_id`. Bounded `[0, 2]`: unbounded, a mistyped 4.36 for 0.436 puts
every cat AND every stranger below the cutoff, silently, across all history.

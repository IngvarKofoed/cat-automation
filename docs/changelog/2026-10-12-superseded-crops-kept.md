# Superseded crops are kept so a geometry move is reversible

<!-- entry 447 -->

Superseded crops are KEPT, for the CLI too, so a geometry move is reversible without a
source frame — and returning to an arm already visited is a pure relink, long after those
frames evicted. That is what makes A/B-ing several crop shapes a sequence of quick hops.
Cost: one copy of the labelled set per shape tried, outside the frame store's byte cap,
reclaimed only by hand. `old_files_removed` became `old_files_kept`.

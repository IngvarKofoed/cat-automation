# Compute re-runs the gate offline with adjustable parameters

<!-- entry 23 -->

Compute's `MogAnalyzer` re-runs the gate offline with adjustable params over stored frames.
Baseline from Pi's live settings (new `GET /api/edge/config`), candidate from edited knobs.
Windowed/stateful (MOG2 background builds frame-by-frame); results persist to analysis table.
Tunes all six params offline — including var_threshold/learning_rate (stored area alone can't recover).

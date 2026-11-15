# Scorecards headline visit recall over frame recall

<!-- entry 46 -->

Tuning scorecards (Live gate / Baseline / Candidate) now headline **visit recall** as a big
footer % instead of a one-line "Visits" row. Visit recall (caught/total visits) — not frame
recall — is the metric the gate is tuned toward: one caught frame per visit is enough to wake
the GPU, so a wholly-missed visit is the only miss that costs a real trigger. Presentation only.

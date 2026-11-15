# Annotation queue gained Hide confident matches

<!-- entry 233 -->

The annotation queue gained "Hide confident matches" (`uncertain_only`), the filter
ARCHITECTURE always described but the code never had. Queue membership is UNDECIDED
(no `dataset_items` row), NOT uncertain — so a good model does not shrink the queue at
all, and weeks of correctly-named visits bury the few worth attention. `uncertain` was
already computed per visit and merely PRINTED; it is now a filter, applied server-side
BEFORE the page cap (after it, confident visits would still eat the 100 slots).
`hidden_confident` is reported, so a short or empty queue never reads as "nothing left".

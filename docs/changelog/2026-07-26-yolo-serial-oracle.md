# yolo-serial oracle added to A/B the batched YOLO sweep

<!-- entry 54 -->

Added the `yolo-serial` oracle: the SAME YOLO backend in its pre-batching, bare-per-frame
call shape (`YoloAnalyzer(serial=True)`; distinct name, batch_size 1), registered beside
`yolo`/`bsuv`. It A/Bs the batched sweep — run both over one bucket, compare each vs MOG2 in
the scorecard — isolating the batching *code*, not the unpinned ultralytics version (both run
under whatever is installed). Also unified the scorecard's oracle allow-list onto the registry:
`store._SCORECARD_ORACLES` was a hardcoded second copy of the names that 500'd
`/api/tuning/compare` for any newly registered oracle; it now derives from `ANALYZER_NAMES`.

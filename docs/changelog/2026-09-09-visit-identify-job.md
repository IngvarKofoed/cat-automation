# New visit-identify job detects then identifies one visit

<!-- entry 281 -->

New `visit-identify` TrainingManager kind + `POST /api/identify/visit`: detect THEN
identify over ONE visit span, the pair `LiveIdentifyManager._tick` runs per closed
visit, on demand for a visit the always-on workers never covered. No "did YOLO find a
cat" branch — `iter_unidentified` yields only frames with a present verdict, so an
empty visit self-skips. A cancel mid-detect returns NO summary, which is what records
it `canceled` rather than `done`.

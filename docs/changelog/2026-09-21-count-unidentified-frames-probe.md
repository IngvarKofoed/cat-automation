# count_unidentified cost is the frames probe, not the mis-plan

<!-- entry 339 -->

That 4.9 s is NOT the entries 229/265/276/307 mis-plan: the plan already seeks
`idx_analysis_analyzer_verdict` to the detected minority (1 ms). The cost is the
per-row `frames` probe, and that join is NOT removable — `iter_unidentified` selects
`f.path` and joins identically, and the count must match its yield exactly, so an
orphan verdict would leave progress short of 100% forever.

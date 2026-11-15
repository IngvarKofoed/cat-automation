# Windowed sweeps batch verdict writes to avoid lock contention

<!-- entry 105 -->

Windowed analysis sweeps (MOG2/BSUV tuning) batch their verdict writes instead of committing per frame.
Per-frame `commit` made the sweep grab the store's shared write lock once per frame, each contending
with the collector's continuous inserts — the dominant cost behind a tuning sweep crawling at ~0.4 fps
(decode+MOG2 alone run ~100 fps; the gap was lock contention, not compute). Now verdicts accumulate and
flush every `_WRITE_BATCH` (256) in one lock hold + commit, plus a final/cancel flush. Safe because a
windowed sweep revisits every frame each run, so verdicts lost to a cancel before their flush are
recomputed next run. Inference stays strictly in-order (MOG2/BSUV are stateful).

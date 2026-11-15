# Store runs WAL with synchronous NORMAL, accepting orphaned JPEGs

<!-- entry 42 -->

Store opened WAL + synchronous=NORMAL store-wide — commits get cheap (fsync deferred to checkpoint).
Accepted consequence: a hard power loss may orphan a JPEG (row lost, file kept) that is never
counted/evicted — a small non-self-healing disk leak, never corruption.
New batched write_analysis_batch and Store.close() (checkpoints on exit). Shutdown now stops AND JOINS both the
collector and the analysis worker before store.close() — both write the one shared connection, so closing under a live writer races a closed DB.

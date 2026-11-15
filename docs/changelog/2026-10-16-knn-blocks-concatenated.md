# kNN blocks are concatenated so miscovered rows raise

<!-- entry 465 -->

The kNN pass builds `nn` by CONCATENATING its blocks rather than assigning into a
pre-sized `np.empty`. A loop that failed to cover every row left uninitialised memory
behind, so the same bug read as a wrong `knn.accuracy` on one machine and as nothing
at all on another — it survived a deliberate stride injection here because the
allocator handed back the previous run's buffer. Miscovering now changes the row
count, which raises.

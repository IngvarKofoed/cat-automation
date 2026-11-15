# Cold count runs on the shared write connection, limit

<!-- entry 567 -->

Known limit: the cold count runs on the shared write connection (entry 354), at most once per
10 minutes per model; the spec estimates ~8 s on the real store. Measured only on a 7k-frame
dev fixture here — the real-store timing, and the spec's dated checks, need the compute PC.

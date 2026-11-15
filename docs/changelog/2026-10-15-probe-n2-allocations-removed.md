# Probe no longer materialises triu index pairs or n-squared argsort

<!-- entry 460 -->

Sizes each one removed, at n=27,011: `triu_indices`' int64 index PAIR is 5.8 GB —
larger than the 1.5 GB of distances it addresses, and gathered through twice per cap;
`np.argsort(dist, axis=1)` allocates n² int64 (5.8 GB) to keep n×k of it; and
`dist = 1.0 - sim` as two statements keeps both matrices alive. The kNN's diagonal is
now masked on `dist` itself and restored from a `.copy()` — `np.diagonal` returns a
VIEW, so saving without copying restores the `+inf` it just wrote.

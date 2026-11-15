# Persisted embedding cache with rescore deliberately not built

<!-- entry 432 -->

Deliberately NOT built: a persisted embedding cache with a `rescore` entry point. Every
question here is a mask over a matrix the run already holds, a preprocessing arm needs
a fresh embed regardless, and paired comparison comes from the per-visit outcome list —
so it bought asking a new cap value later in exchange for a second scoring path to keep
byte-identical forever. Reversed mid-spec after review; the earlier claim that it made
paired comparison possible was simply wrong.

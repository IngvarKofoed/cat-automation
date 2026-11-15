# Orphan sweep confirms a negative with the path equality before deleting

<!-- entry 312 -->

The orphan sweep CONFIRMS a negative with the unindexed `path` equality before deleting.
Entry 307's fast probe rests on a filename parse, and this call removes FILES — so a parse
returning a wrong-but-plausible millisecond would find no row and destroy live frames.
Proven with a deliberately broken parser: 0 deleted instead of 20,000. The scan runs only
for candidates that already look orphaned (rare — orphans exist only after a crash), so
the normal sweep is unchanged at 0.49 s/20k files. Made structural because the failure
would be silent, total, and is newly AUTOMATIC at launch.

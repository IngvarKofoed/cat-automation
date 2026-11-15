# Corruption review page with a non-registered analyzer

<!-- entry 97 -->

Corruption review page (`#corruption`): a NON-registered persisted `CorruptionAnalyzer` wraps the shared
guard and is swept via the shared job queue (own `/api/corruption/run`; absent from `ANALYZER_NAMES` so it
never leaks into scorecard/disagreement/oracle paths). `/api/corruption` reads the stored `corruption` flag
to filter a range for corrupt, and the fail-non-safe danger set (corrupt AND cat), with staleness (verdicts
predating a `_CORRUPT_*` change) and cat-coverage warnings so an empty danger set never reads as "safe".
Caveat: the guard forces corrupt→motion=0, so a motion-only sweep can't find corruption in post-guard
frames. Spec: docs/specs/2026-07-23-corruption-review-page.md.

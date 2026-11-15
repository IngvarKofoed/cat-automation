# code-review verifiers run on the session model, correction

<!-- entry 230 -->

Corrected the root CLAUDE.md claim that `code-review`'s verifiers "default to the mid
model": the built-in script sets NO `model:` anywhere, so every stage runs on the
session model. A `high` run over this ~1000-line diff spent ~743k subagent tokens and
hit the weekly limit before ANY finder returned — and reported `findings: []`, which
reads as "clean" rather than "never ran". Tier the verify fan-out by hand; note the
script is a per-run copy, and re-tiering a COMPLETED agent voids its resume cache.

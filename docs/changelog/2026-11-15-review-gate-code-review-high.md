# Commit gate offers /code-review high --fix, not /fix-code

The commit-time review gate now offers `/code-review high --fix` or commit now. It replaces
the `/fix-code --fix` preference and its hand-run inline fallback
(`2026-08-31-fix-code-preferred-review`). The gate itself — one review point, at the commit
(`2026-09-01-review-only-at-commit`) — is unchanged.

## Detail
- Reverses `2026-08-31-code-review-not-suggested`: `/code-review` then needed a PR and only
  posted a comment. It is now model-invocable, reviews the working diff and applies `--fix`.
- Cost is the open risk: a `high` run once spent ~743k subagent tokens and returned nothing
  (`2026-08-30-code-review-no-tiering-correction`). Its verify fan-out is still untiered, so
  the Multi-agent section's per-run tiering note still applies.
- The repo's risk-surface list (eviction, the write lock, `shared/`, the durable tables,
  actuation) is kept, now named at the gate instead of assigned to a per-dimension finder.

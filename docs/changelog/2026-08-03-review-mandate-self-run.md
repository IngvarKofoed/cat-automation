# Review mandate reframed as the agent's own self-run pass

<!-- entry 93 -->

Root CLAUDE.md review mandate reframed to match reality: the per-edit pass is the agent's OWN self-run
review (single read for small edits; Agent-tool subagent fan-out with verified findings for big ones),
NOT an invocation of the `/code-review` skill — that skill is user-invoke-only (`disable-model-invocation`),
so the old "invoke code-review --fix" mandate silently failed every edit. Grouped-severity reporting and
the user-run `/code-review medium` nudge are unchanged. Also: the ultracode section now notes a returning
workflow's aggregate diff triggers that same self-review (no single subagent saw the whole change).

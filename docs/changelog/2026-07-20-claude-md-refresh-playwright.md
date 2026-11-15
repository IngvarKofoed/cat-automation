# CLAUDE.md refreshed, review nudge added, Playwright MCP named

<!-- entry 27 -->

CLAUDE.md guidance refreshed to current scaffold conventions. Root code-review mandate now ends a
significant change by suggesting a deliberate user-run `/code-review medium` pass — the single
auto-`--fix` pass is never re-reviewed, so a big diff still gets a human second look.
Edge/compute UI-verification now name the installed Playwright MCP (`mcp__playwright__*`),
replacing the uninstalled `claude-in-chrome` that couldn't actually be loaded here.

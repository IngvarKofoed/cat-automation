# Cat Automation — shared

The contracts both tiers agree on: the data model, the event/intent schemas, the Pi control-API shape, and shared constants. Deliberately small — see `docs/ARCHITECTURE.md`.

Contents: data-model definitions, event & intent schemas, the control-API shape, and shared constants.

## Required tools

- **`LSP`** — Python symbol navigation, references, and hover. Load if deferred: `ToolSearch select:LSP`. (Python support is provided by the installed `pyright-lsp` plugin — Pyright.)

## Testing

**pytest** for any validation / serialization logic. Most of this subtree is declarative, so test the parts that can break: schema round-trips, invariants, and backward-compatible changes. Do not introduce a different test framework without updating the architecture doc.

## Subtree-scoped rules

- **This is a contract, not just code.** Both the `edge/` and `compute/` tiers depend on everything here. Changing a schema, an intent, or the control-API shape is a **breaking change across the wire**: update *both* sides in the same change, and update `docs/ARCHITECTURE.md` (data model / intents / control API) so the docs stay the source of truth. Never change a shared contract in isolation.
- **Keep it small and dependency-light.** No tier-specific imports here (no `Picamera2`, no `torch`). If only one tier needs something, it belongs in that tier — not in `shared/`.

## Required skills

None beyond the global `code-review` mandate (in the root `CLAUDE.md`).

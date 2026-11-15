# Cat Automation — shared

The contracts both tiers agree on — the data model, the event/intent schemas, the Pi control-API shape, shared constants — and the cross-tier **logic** both tiers instantiate: the `MotionGate` core, so the compute tier re-runs the edge's exact gate offline with no drift from the live one, and the frame wire format. Deliberately small — see `docs/ARCHITECTURE.md`.

Contents: `motion.py` (the `MotionGate` core), `wire.py` (the frame wire format), `tests/`. The data model, event/intent schemas and control-API shape belong here too once they are written down as code; today they live in `docs/ARCHITECTURE.md`.

## Required tools

- **`LSP`** — Python symbol navigation, references, and hover. Load if deferred: `ToolSearch select:LSP`. (Python support is provided by the installed `pyright-lsp` plugin — Pyright.)

## Testing

**pytest** for any validation / serialization logic. Most of this subtree is declarative, so test the parts that can break: schema round-trips, invariants, and backward-compatible changes. Do not introduce a different test framework without updating the architecture doc.

## Subtree-scoped rules

- **This is a contract, not just code.** Both the `edge/` and `compute/` tiers depend on everything here. Changing a schema, an intent, or the control-API shape is a **breaking change across the wire**: update *both* sides in the same change, and update `docs/ARCHITECTURE.md` (data model / intents / control API) so the docs stay the source of truth. Never change a shared contract in isolation.
- **Keep it small and dependency-light.** No tier-specific imports here (no `Picamera2`, no `torch`). If only one tier needs something, it belongs in that tier — not in `shared/`.

## Required skills

None beyond the commit-time review gate in the root `CLAUDE.md`.

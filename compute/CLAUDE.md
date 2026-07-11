# Cat Automation — compute (NVIDIA PC)

The brain. It connects to the Pi's stream and does all the intelligence — cat detection, tracking, individual re-ID identification, direction, the decision engine, the event store, notifications, the dashboard, and the human-in-the-loop learning loop. See `docs/ARCHITECTURE.md`.

Contents: `ingest/` (stream client), `detection/` (YOLO cat detection), `tracking/` (tracker + direction resolver), `identification/` (embedding model + gallery, open-set), `decision/` (decision engine → intents), `store/` (event store + occupancy state), `notify/` (push notifier), `api/` (dashboard backend + `web/` frontend), `learning/` (collection, annotation queue, training + promotion), `dataset/` (crops + labels), `models/` (versioned artifacts).

## Required tools

- **`LSP`** — Python symbol navigation, references, and hover across the compute code. Load if deferred: `ToolSearch select:LSP`. (Python support is provided by the installed `pyright-lsp` plugin — Pyright.)
- **Browser automation (Playwright MCP)** — for verifying the **dashboard UI** (and the existing browse/tuning UI) in a real browser. The tools are deferred `mcp__playwright__*`; load them via `ToolSearch` (keyword `playwright browser`, or `select:mcp__playwright__browser_navigate,mcp__playwright__browser_snapshot,mcp__playwright__browser_console_messages,mcp__playwright__browser_network_requests`). Not needed for ML/backend work.

## Testing

Python unit and integration tests use **pytest**. GPU-/model-dependent tests should run against small fixtures or be skippable without a GPU, so the suite is runnable anywhere. The dashboard frontend, when built, brings its own test framework per its stack (`api/web/`). Do not introduce a different test framework without updating the architecture doc.

## Subtree-scoped rules

- **Phase 1 is the feasibility question.** The near-term goal is images out, good background/reference handling, and basic collection + training to answer *"can we tell our cats apart at all?"* Favor a measurable end-to-end pipeline over polish; there is no actuation yet.
- **Protect the gallery.** Build the recognition gallery from *clean, representative* crops of each cat. Route blurry / extreme-angle hard cases to threshold-tuning and validation, **not** into the gallery — folding them in blurs the embedding space and lets a stranger's bad crop match a resident.
- **Confidence isn't free.** Raw embedding distance is not calibrated confidence; tune the threshold(s) against real collected data rather than trusting a default.
- **Verification workflow.**
  - *ML / backend changes:* the pytest suite **plus** running the actual pipeline over sample images and inspecting the result (detections, identities, confidences).
  - *Dashboard / browse-UI changes:* 1. start the relevant FastAPI app locally; 2. drive the changed view in a real browser via the Playwright MCP (`mcp__playwright__*`) tools; 3. check console messages + network requests for errors; 4. only then report complete.

## Required skills

For the **dashboard UI phase** (not needed for the current ML/backend work):

- **`frontend-design`** — invoke when building or reshaping the dashboard UI, for distinctive, non-templated design.
- **`dataviz`** — invoke *before* writing any dashboard chart, timeline, or occupancy visualization.

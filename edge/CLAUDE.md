# Cat Automation — edge (Raspberry Pi)

The thin smart-camera node at the door. It captures video, clips it to the ROI, runs simple motion detection, and serves everything over one HTTP server; it drives whatever actuators are installed. It holds **no ML models** and makes **no recognition decisions** — see `docs/ARCHITECTURE.md`.

Contents: `capture/` (capture-source interface + backends: CSI/USB/IP), `clip/` (ROI cropping), `motion/` (motion gate + dynamic background), `server/` (HTTP: `/stream`, `/frame`, control API, config UI + `ui/` assets), `actuators/` (lock/sound/light GPIO drivers, optional), `config/` (persisted camera + motion settings).

## Required tools

- **`LSP`** — Python symbol navigation, references, and hover across the edge code. Load if deferred: `ToolSearch select:LSP`. (Python support is provided by the installed `pyright-lsp` plugin — Pyright.)

## Testing

Unit tests live alongside the edge packages using **pytest**. The edge is I/O- and hardware-bound (camera, GPIO), so keep the hardware behind the capture-source and actuator interfaces and test against fakes/stubs; the real camera and GPIO are validated on-device. Do not introduce a different test framework without updating the architecture doc.

## Subtree-scoped rules

- **Keep the Pi thin.** It must hold no ML models and make no recognition decisions — that is a guiding principle, not a convenience. Detection and identification live on `compute/`. If you're tempted to run inference here, stop: that's an architecture change, not an edge change.
- **The Pi is a pure server.** It only ever *listens* (HTTP inbound: stream, control, config); it never dials out. Preserve that — no outbound connections from the edge.
- **Verification workflow (on-device / against fakes).** The camera and GPIO can't be meaningfully unit-tested, so verify edge changes by running the service (on the Pi, or against a fake capture source) and:
  1. Hit `/frame` and `/stream`; confirm frames flow — and that the stream only emits while there is motion.
  2. Exercise the control API endpoints; confirm the actuator (or its no-op stub) responds.
  3. Confirm the config UI reflects and persists settings (clip, focus, fps, background).
  4. Only then report the change complete. Keep it light — this is a prototype.

## Required skills

None beyond the global `code-review` mandate (in the root `CLAUDE.md`).

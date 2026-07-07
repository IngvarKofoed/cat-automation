# Changelog

Each entry is numbered with a monotonically increasing integer. Append new entries to the end. Never reuse or reorder numbers. Numbers are globally unique across this file and any future `CHANGELOG-archive.md` — never reused. Write each entry as durable project memory: what is now true that wasn't before, plus the why in a clause when not obvious — not a recap of the diff (filenames and mechanical edits live there). Keep it to 1–5 lines, ~20 words per line at most; never one packed run-on line.

1. Established the anchoring docs and CLAUDE.md scaffolding for the cat-door vision prototype:
   CONCEPT (why/what), ARCHITECTURE (how — thin Pi edge streaming MJPEG to an NVIDIA compute PC),
   and root + edge/compute/shared CLAUDE.md.
   Framed as an early prototype on a trusted LAN: no auth between components, and door actuation
   plus its access-decision policy are deferred to a later phase.

2. Edge tier first slice: Flask `/frame` (JPEG q90), config UI with Capture button, API (GET /api/cameras, GET|POST /api/config).
   Pluggable CaptureSource (edge/capture/) with OpenCV backend; device id opaque (int or /dev/video* path) avoids lossy conversion.
   FakeCaptureSource for tests. Persistent capture self-heals on failure; device switches new-before-close under lock.

3. `POST /api/config` now returns 422 (not 503) when the candidate device fails to open.
   503 stays reserved for `/frame`'s already-working camera failing at read time; a rejected
   device selection is a client-input problem, not a service outage — per the MVP spec.

4. Edge hardened after code review. A hand-edited or corrupt `settings.json` can no longer
   crash boot: invalid or non-object values fall back to defaults, and `POST /api/config`
   rejects a non-object body with 400 (was 500). Device switch now persists *before* the
   in-memory swap and outside the slot lock, so a failed write can't leak the old capture
   handle or diverge live-vs-saved state. Config UI always shows the active device even when
   enumeration omits it (default index 0 vs Linux `/dev/video*`).

5. `./edge.sh` is the entrypoint to run the edge server: it bootstraps `.venv` from
   `edge/requirements.txt` on first run, then launches `edge.server.app` (honors
   `CAT_EDGE_PORT`). One command to start the edge on a fresh checkout.


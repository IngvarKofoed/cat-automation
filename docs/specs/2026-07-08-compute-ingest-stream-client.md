# Compute ingest / stream client + shared wire contract

The first compute-tier code: a client in `compute/ingest/` that opens the Pi
edge server's `GET /stream` (continuous `multipart/x-mixed-replace` MJPEG),
parses each part into a decoded frame plus its motion / frame-id / timestamp
metadata, and reads `GET /status` as the authoritative camera-health and
liveness signal. Alongside it, the edge↔compute wire contract — the stream
part-header names, the `/status` JSON shape, and the frame-identity semantics —
is lifted out of the edge's hand-written byte literals into `shared/wire.py`, a
single module both tiers import: the edge *serializes* through it and compute
*parses* through it, so the two sides cannot drift.

## Key decisions

- **`shared/wire.py` is the single source of truth, consumed by both tiers**
  (new). Header/field-name constants, typed `StatusSnapshot` / `StreamFrameMeta`
  snapshots, and pure (no-I/O, no-cv2) parse **and** serialize helpers live here.
  `shared/` becomes an importable package (`shared/__init__.py`). This is what
  makes the contract real rather than a doc — see `shared/CLAUDE.md` ("a
  contract, not just code").
- **The edge is refactored to serialize its wire bytes through `shared/wire.py`**
  (extends; one deliberate wire change). `_build_part` (`edge/server/app.py:288`),
  the `/status` handler, and the `/stream` mimetype stop hand-writing the format as
  byte literals and go through `shared/wire.py` — which owns the **whole** part
  header block, framing lines included (boundary, `Content-Type`, `Content-Length`,
  and the `X-*` headers), so the client frames each body via the same definition
  the edge wrote it with. The bytes are identical **except one intentional
  addition**: the edge now emits `X-Area` on *every* part (was: only when motion
  is active), so the stream matches `/status` and the grabber's always-reported
  `area`. A round-trip test (`format → parse == identity`) locks the rest, and
  drift is now impossible. The edge gains a dependency on `shared` (pure-Python,
  no new packages).
- **Manual multipart parse over a streamed `requests` GET — not
  `cv2.VideoCapture`** (new). `VideoCapture(url)` (which `ARCHITECTURE.md`
  name-checks) hands back decoded frames only and **discards the part headers**,
  losing `X-Frame-Id`/`X-Timestamp` (frame identity) and `X-Motion` (inline
  motion) — the very signals this client exists to carry. So we parse the
  multipart stream ourselves and `cv2.imdecode` the JPEG bodies.
- **Lazy JPEG decode** (new). A yielded `StreamFrame` carries its
  `StreamFrameMeta` plus the raw JPEG `bytes`, and decodes to an ndarray only on
  first `.image` access (cached). A frame the consumer skips on the motion signal
  costs zero decode. `cv2`/`numpy` are imported lazily in that path, so the
  parsing and liveness logic stay unit-testable without the CV stack.
- **`/status` is the health/liveness oracle; the stream is the data plane** (new;
  realizes the one-way liveness design in `ARCHITECTURE.md`). Camera health comes
  from `/status.camera_ok`; a failed `/status` request or a dropped stream
  connection means the Pi/network is down. A stream *stall* (no new part within a
  read timeout) triggers a reconnect, not a health verdict on its own.
- **`requests` + a new `compute/requirements.txt`** (new). This is the first
  compute code, so it seeds the file, pinned `requests>=2.31` (unpinned upper
  bound, matching the prototype's light-touch deps). `requests` is the ergonomic
  sync choice for one long-lived stream; async (`httpx`) buys nothing for a single
  connection.
- **Sync blocking iterator; the caller owns threading** (new). `iter_stream()`
  is a plain generator; `get_status()` is a plain call. An opt-in
  reconnect-with-backoff wrapper is provided for callers that just want an endless
  frame stream, but the raw iterator surfaces `EdgeUnavailable` so liveness stays
  explicit rather than silently swallowed.

## Goals

- A compute-side client that yields decoded frames from the Pi's `/stream`, each
  carrying its `frame_id`, `ts`, and motion (`motion`/`bbox`/`area`), and that
  reads `/status` for camera health + host metrics.
- Formalize the edge↔compute wire contract in `shared/`, with both tiers using it
  so the format has exactly one definition.
- Let the compute learn the Pi's liveness and the camera's health from its own
  connections, per the architecture's one-way liveness model.
- Be verifiable without a running Pi or a GPU (pure parsers over byte fixtures;
  the stream client against the in-process edge app / a mocked response).

## Non-goals

- Cat detection, tracking, identification, or any storage — those are the
  *consumers* of these frames, in later specs. Ingest stops at "frames + metadata
  + health, out."
- The **control-plane** client (`POST lock`/`unlock`/`sound`/`light`) — deferred
  to the actuation phase. This is data-plane only (`/stream` + `/status`).
- Offline tolerance: buffering clips during an outage and backfilling the record
  when the Pi returns. The client reconnects; it does not backfill.
- Idempotent event de-duplication by `frame_id` — that belongs to the event
  store. Ingest only *surfaces* `frame_id`; it does not act on it.
- Multiple cameras / multiple Pis; async I/O.

## Design

### `shared/wire.py`

The contract, as data + pure functions. No `requests`, no `cv2`, no Flask.

- **Constants.** The multipart boundary token (`frame`), the stream part header
  names (`X-Frame-Id`, `X-Timestamp`, `X-Motion`, `X-Bbox`, `X-Area`), and the
  `/status` field names — each a named constant, referenced by both tiers.
- **`StreamFrameMeta`** (`NamedTuple`, mirroring the edge's `FrameSnapshot`
  idiom): `frame_id: int`, `ts: int`, `motion: bool`, `bbox: tuple | None`,
  `area: float`. `area` is always present (the edge emits `X-Area` on every part,
  `0.0` when there is no blob); `bbox` is `None` when motion is inactive — no blob
  means no box, so the edge sends `X-Bbox` only while motion is active.
- **`StatusSnapshot`**: `frame_id`, `ts`, `motion`, `bbox`, `area`, `camera_ok`,
  `last_error`, `version`, `system` (`dict | None`) — the exact `/status` JSON.
  `parse_status` converts the JSON `bbox` list to a 4-float tuple, so
  `StatusSnapshot.bbox` and `StreamFrameMeta.bbox` are the same type.
- **Serialize (edge side):** `format_part_headers(meta, content_length: int) ->
  bytes` producing the **entire** part header block — boundary separator,
  `Content-Type`, `Content-Length` (hence the `content_length` arg, which the meta
  can't carry), and the `X-*` headers. The edge keeps its overlay/JPEG-encoding
  logic and passes the encoded body's length; all header bytes come from here.
- **Parse (compute side):** `parse_part_headers(block: bytes) -> (StreamFrameMeta,
  content_length)` — returns the framing length alongside the meta so the client
  reads exactly one body through the same definition the edge serialized — and
  `parse_status(obj: dict) -> StatusSnapshot`. `X-Motion` → bool; `X-Bbox`
  `"x,y,w,h"` → 4-float tuple; `X-Area` → float; tolerant of the motion-inactive
  case (missing `X-Bbox`). Unknown headers/fields are ignored so an additive
  contract change (like `system` was) doesn't break an old parser.
- **Malformed vs missing.** A **malformed** required field — non-integer
  `X-Frame-Id`, un-parseable `X-Bbox`, absent `X-Motion` — raises a typed
  `WireParseError` rather than guessing; the stream client treats that like a
  stall (stream corruption → reconnect). A **missing** field is read as its
  neutral default (`X-Area → 0.0`, `X-Bbox → None` — so a pre-`X-Area`-always edge
  still parses; on `/status`, `system → None`, `bbox → None`, `version →
  "unknown"`), distinct from an *unknown extra* field, which is ignored.

Semantics documented here, since this file is the contract:
- `frame_id` is the ordering/identity key — monotonic, advances only on a
  successful grab. **Order and dedupe by `frame_id`, never by arrival order.**
- `ts` is wall-clock epoch-ms and may jump (the Pi has no RTC; NTP steps it after
  boot). It is for logging/display only — never derive deltas or ordering from it.

### Edge refactor (`edge/server/app.py`)

`_build_part` builds its **entire** header block by calling
`format_part_headers(meta, len(data))` (it currently hand-writes the boundary,
`Content-Type`, `Content-Length`, and `X-*` lines); the `/status` handler names
its JSON keys from the shared constants; and the `/stream` route's
`Response(..., mimetype="multipart/x-mixed-replace; boundary=…")` sources the
boundary token from the shared constant too. The boundary appears in **two** edge
spots — the part separator *and* the mimetype declaration — so both must come from
the one constant or they can silently desync.

The one intended behavior change: `_build_part` now emits `X-Area` on every part
(previously only when motion was active), matching `/status` and the grabber's
`area` "always reported for tuning" contract. So the stream parts are byte-for-byte
what they are today for the **motion-active** case and differ only by the added
`X-Area` line when **inactive**. Lock this with a test asserting the exact
produced bytes for both cases (including the float formatting of
`X-Bbox`/`X-Area`); the existing stream test in `edge/tests/test_app.py` only
checks header *presence* (and asserts `X-Area` absent when idle), so update those
assertions and add the byte-exact check — genuinely new and load-bearing.

### `compute/ingest/`

An `EdgeClient(base_url=None)` — base URL from the constructor, falling back to
the `CAT_PI_URL` env var (the compute tier has no config store yet); if neither is
set the constructor raises a clear configuration error rather than deferring a
`None`-into-URL crash to first use. One `iter_stream()` and one `get_status()` are
safe to run concurrently from different threads — each request is independent and
`get_status()` never touches the open stream:

- **`iter_stream() -> Iterator[StreamFrame]`.** Opens `GET {base_url}/stream`
  with `requests` (`stream=True`, a `(connect, read)` timeout where the read
  timeout is the stall threshold — 5 s default, constructor-overridable). Wraps
  `resp.raw` in a buffered reader: `readline()`s the boundary line (`--` + the
  shared boundary constant) and the header lines through the blank line, hands the
  block to `parse_part_headers` → `(meta, content_length)`, then
  `read(content_length)` for the JPEG body (Content-Length framing is robust; the
  edge always sends it). Yields `StreamFrame(meta, jpeg_bytes)`. A read timeout
  (stall) or connection drop raises `EdgeUnavailable`.
- **`StreamFrame`**: `.meta` (`StreamFrameMeta`), `.jpeg` (`bytes`), and a cached
  `.image` property that `cv2.imdecode`s on first access.
- **`get_status() -> StatusSnapshot`.** `GET {base_url}/status`, `parse_status`
  the JSON. Raises `EdgeUnavailable` on a connection error / non-200. Caller
  drives the cadence — no internal timer.
- **`iter_stream_reconnecting(...)`.** A thin wrapper that re-opens the stream on
  `EdgeUnavailable` with exponential backoff (0.5 s → 10 s cap, with jitter), for
  callers that just want frames. It cannot distinguish *why* the stream dropped —
  Pi down, network down, or a wedged camera on a healthy Pi (which stalls the
  stream but keeps `/status` answering `camera_ok=false`) all look identical — so a
  consumer that needs to know the camera died must still poll `get_status()`.

### Cross-tier docs

`ARCHITECTURE.md` describes the stream part headers as carrying `X-Bbox`/`X-Area`
"when active"; update it to say `X-Area` is **always** present (`X-Bbox` only when
motion is active), since that section is the source of truth for the contract.
Changed in the same commit as the wire change.

### Verification (described; build phase)

- `shared`: pytest over the pure functions with captured byte fixtures — a real
  part-header block (motion-active and inactive), a real `/status` payload — plus
  the round-trip `format → parse == identity` test that locks edge↔compute.
- `compute/ingest`: feed the parser a canned multipart byte stream; and an
  integration test that runs the real edge app in-process over `FakeCaptureSource`
  and points `EdgeClient` at it (no camera, no GPU).

## Alternatives considered

- **`cv2.VideoCapture(url)` for the stream.** Simplest to write and explicitly
  mentioned in `ARCHITECTURE.md`, but it discards the multipart part headers, so
  frame identity and inline motion are lost — the two things the pull-signal
  design puts *in* those headers. Rejected.
- **Contract in `shared/`, but only compute wired to it now (edge unchanged).**
  Lower blast radius, but the edge's byte-literal headers and `shared` could
  drift until a later edge refactor — the precise failure `shared` exists to
  prevent. Rejected in favor of refactoring the edge in the same change.
- **Contract kept inline in `compute/ingest/`, no `shared`.** Contradicts the
  goal of formalizing the cross-tier contract; leaves the edge as the only
  definition. Rejected.
- **`httpx` / async.** No benefit for a single long-lived stream; adds an async
  surface the rest of the (sync) compute code doesn't need. Rejected.

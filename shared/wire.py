"""The edge↔compute wire contract: one definition both tiers bind to.

The Pi edge streams frames as ``multipart/x-mixed-replace`` MJPEG and answers a
JSON ``GET /status`` health/motion poll (see ``docs/ARCHITECTURE.md`` —
"Communication and data flow"). Historically the edge hand-wrote those bytes as
literals in ``edge/server/app.py``, which meant the *only* definition of the
format lived inside the serializer; a compute-side parser could silently drift
from it. This module is the fix: the edge **serializes** through the helpers
here and the compute tier **parses** through them, so the two sides share a
single, testable definition of the format and cannot desync. A round-trip test
(``format_part_headers`` → ``parse_part_headers`` == identity) locks it.

Kept intentionally pure — no ``requests``, no ``cv2``, no Flask — so it is
trivially unit-testable and adds no dependency to either tier. It carries only
the *shape* of the wire: header/field names, the typed snapshots, and the
serialize/parse functions over raw bytes and plain dicts. Decoding JPEG bodies
and doing HTTP I/O belong to the tiers, not to the contract.

Frame-identity semantics (documented here because this file *is* the contract):

- ``frame_id`` is the ordering/identity key — monotonic, advancing only on a
  successful grab. Order and dedupe by ``frame_id``, **never** by arrival order.
- ``ts`` is wall-clock epoch-ms and may jump (the Pi has no RTC; NTP steps it
  after boot). It is for logging/display only — never derive deltas or ordering
  from it.

Tolerance rules keep old and new peers interoperable:

- A **malformed** required field (non-integer ``X-Frame-Id``, un-parseable
  ``X-Bbox``, absent ``X-Motion``) raises ``WireParseError`` rather than
  guessing — the stream client treats that like stream corruption and reconnects.
- A **missing** field is read as a neutral default (``X-Area`` → ``0.0``,
  ``X-Bbox`` → ``None``; on ``/status`` also ``system`` → ``None``, ``bbox`` →
  ``None``, ``version`` → ``"unknown"``), so a *pre-``X-Area``-always* edge or a
  *pre-``system``* edge still parses.
- An **unknown** extra header/field is ignored, so an additive contract change
  (as ``system`` once was) never breaks an existing parser.
"""
from __future__ import annotations

from typing import NamedTuple

# --- Multipart framing ------------------------------------------------------
# The boundary token appears in TWO places on the edge — the ``multipart/...;
# boundary=<TOKEN>`` mimetype and each part's ``--<TOKEN>`` separator line — so
# both must source it from this one constant or they can silently desync.
BOUNDARY = "frame"

# --- Stream part header names -----------------------------------------------
# The X-* headers carry the pull signal (frame identity + motion) inline on each
# streamed part, so a stream consumer reads motion without a separate poll.
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_CONTENT_LENGTH = "Content-Length"
HEADER_FRAME_ID = "X-Frame-Id"
HEADER_TIMESTAMP = "X-Timestamp"
HEADER_MOTION = "X-Motion"
HEADER_BBOX = "X-Bbox"
HEADER_AREA = "X-Area"

# The JPEG part body's declared content type (parts are always JPEG on the wire;
# lossless stills are a /frame concern, not a /stream one).
CONTENT_TYPE_JPEG = "image/jpeg"

# --- /status JSON field names -----------------------------------------------
FIELD_FRAME_ID = "frame_id"
FIELD_TS = "ts"
FIELD_MOTION = "motion"
FIELD_BBOX = "bbox"
FIELD_AREA = "area"
FIELD_CAMERA_OK = "camera_ok"
FIELD_LAST_ERROR = "last_error"
FIELD_VERSION = "version"
FIELD_SYSTEM = "system"

# CRLF is the multipart line terminator; spelled out so the byte layout is
# unmistakable and never accidentally a bare "\n".
_CRLF = b"\r\n"


class WireParseError(Exception):
    """A required wire field is absent or malformed (corruption, not I/O).

    Raised by the parsers when the bytes/JSON cannot be trusted — a non-integer
    ``X-Frame-Id``, an un-parseable ``X-Bbox``, an absent ``X-Motion``, etc. The
    stream client treats this like a stream stall: drop the connection and
    reconnect rather than emit a half-parsed frame.
    """


class StreamFrameMeta(NamedTuple):
    """The per-part metadata carried in a stream part's ``X-*`` headers.

    Mirrors the edge grabber's frame slot. ``area`` is ALWAYS present (the edge
    emits ``X-Area`` on every part, ``0.0`` when there is no motion blob), which
    is why it is a plain ``float`` and not optional. ``bbox`` is ``None`` when
    motion is inactive — no blob means no box, so the edge sends ``X-Bbox`` only
    while motion is active. When present, ``bbox`` is a 4-tuple ``(x, y, w, h)``
    of floats normalized to the ROI.
    """

    frame_id: int
    ts: int
    motion: bool
    bbox: "tuple[float, float, float, float] | None"
    area: float


class StatusSnapshot(NamedTuple):
    """The full ``GET /status`` JSON, typed.

    Same motion fields as ``StreamFrameMeta`` (``parse_status`` converts the JSON
    ``bbox`` LIST to a tuple so both snapshots use the same ``bbox`` type) plus
    the camera-health/liveness fields the compute tier uses as its authoritative
    health oracle: ``camera_ok`` (fresh, error-free frame), ``last_error``, the
    edge ``version``, and ``system`` (host CPU/mem, ``None`` when ``psutil`` is
    unavailable on the edge).
    """

    frame_id: int
    ts: int
    motion: bool
    bbox: "tuple[float, float, float, float] | None"
    area: float
    camera_ok: bool
    last_error: "str | None"
    version: str
    system: "dict | None"


def _parse_bbox(raw: str) -> "tuple[float, float, float, float]":
    """Parse an ``X-Bbox`` value ``"x,y,w,h"`` into a 4-float tuple.

    Raises ``WireParseError`` on the wrong field count or a non-float component —
    a present-but-garbage bbox is corruption, not a missing field.
    """
    parts = raw.split(",")
    if len(parts) != 4:
        raise WireParseError(f"{HEADER_BBOX} expects 4 comma-separated values, got {raw!r}")
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError as exc:
        raise WireParseError(f"{HEADER_BBOX} has a non-numeric component: {raw!r}") from exc
    return (x, y, w, h)


def format_part_headers(meta: StreamFrameMeta, content_length: int) -> bytes:
    """Serialize one stream part's ENTIRE header block (edge side).

    Returns the boundary separator through the terminating blank line — i.e. the
    bytes that precede the JPEG body — ending in ``CRLF CRLF``. The JPEG body and
    its trailing CRLF are the caller's to append; ``content_length`` is passed in
    because the meta can't carry the encoded body's length.

    The byte layout is fixed and, for the MOTION-ACTIVE case, byte-for-byte
    identical to what ``edge/server/app.py`` emitted historically. The one
    deliberate change: ``X-Area`` is now emitted on EVERY part (not only when
    motion is active), matching ``/status`` and the grabber's always-reported
    ``area``. Ordering is load-bearing for the byte-exactness guarantee: when
    present, ``X-Bbox`` comes BEFORE ``X-Area``.
    """
    # Value formatting matches the edge's historical output exactly: str(int)
    # for the framing/identity headers, "1"/"0" for motion, the raw tuple values
    # for bbox, and str(float) for area.
    block = (
        b"--" + BOUNDARY.encode() + _CRLF
        + HEADER_CONTENT_TYPE.encode() + b": " + CONTENT_TYPE_JPEG.encode() + _CRLF
        + HEADER_CONTENT_LENGTH.encode() + b": " + str(int(content_length)).encode() + _CRLF
        + HEADER_FRAME_ID.encode() + b": " + str(meta.frame_id).encode() + _CRLF
        + HEADER_TIMESTAMP.encode() + b": " + str(meta.ts).encode() + _CRLF
        + HEADER_MOTION.encode() + b": " + (b"1" if meta.motion else b"0") + _CRLF
    )
    # X-Bbox only while motion is active with a blob (no blob → no box). It MUST
    # precede X-Area to preserve the historical byte order.
    if meta.motion and meta.bbox is not None:
        bx, by, bw, bh = meta.bbox
        block += HEADER_BBOX.encode() + b": " + f"{bx},{by},{bw},{bh}".encode() + _CRLF
    # X-Area is always emitted (the intended contract change): the grabber always
    # reports area, so the stream now matches /status regardless of motion.
    block += HEADER_AREA.encode() + b": " + str(meta.area).encode() + _CRLF
    # Terminating blank line separating headers from the (caller-appended) body.
    return block + _CRLF


def parse_part_headers(block: bytes) -> "tuple[StreamFrameMeta, int]":
    """Parse a stream part's header block (compute side).

    Inverse of ``format_part_headers``: takes the boundary-through-blank-line
    bytes and returns ``(StreamFrameMeta, content_length)`` so the client can
    then read exactly ``content_length`` body bytes framed by the same definition
    the edge wrote. Headers are parsed BY NAME (order-independent) and header
    names are matched case-insensitively (HTTP header names are case-insensitive).
    Unknown headers — including the boundary/``Content-Type`` framing lines — are
    ignored.
    """
    headers: dict[str, str] = {}
    for line in block.split(_CRLF):
        if not line:
            continue  # the terminating blank line (and any stray blanks)
        try:
            text = line.decode("latin-1")
        except UnicodeDecodeError as exc:  # defensive: header bytes are ASCII
            raise WireParseError(f"non-decodable header line: {line!r}") from exc
        if ":" not in text:
            continue  # the "--<boundary>" separator or a malformed non-header line
        name, _, value = text.partition(":")
        headers[name.strip().lower()] = value.strip()

    frame_id = _require_int(headers, HEADER_FRAME_ID)
    ts = _require_int(headers, HEADER_TIMESTAMP)
    content_length = _require_int(headers, HEADER_CONTENT_LENGTH)
    motion = _require_motion(headers)

    # X-Bbox: present → parse; absent → None (motion-inactive parts, and pre-
    # always-X-Area edges alike, simply omit it).
    raw_bbox = headers.get(HEADER_BBOX.lower())
    bbox = _parse_bbox(raw_bbox) if raw_bbox is not None else None

    # X-Area: missing → 0.0 (a pre-"X-Area-always" edge omits it when idle);
    # present-but-garbage → corruption.
    area = _parse_float_default(headers.get(HEADER_AREA.lower()), HEADER_AREA, 0.0)

    return StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area), content_length


def parse_status(obj: dict) -> StatusSnapshot:
    """Parse a ``GET /status`` JSON dict into a ``StatusSnapshot`` (compute side).

    Required fields (``frame_id``, ``ts``, ``motion``, ``camera_ok``) raise
    ``WireParseError`` when absent or malformed. ``bbox`` (a JSON list) is
    converted to a 4-float tuple so it matches ``StreamFrameMeta.bbox``. Missing
    fields fall back to neutral defaults (``area`` → ``0.0``, ``bbox`` → ``None``,
    ``system`` → ``None``, ``version`` → ``"unknown"``, ``last_error`` → ``None``)
    so an older edge still parses; unknown extra fields are ignored.
    """
    frame_id = _require_int(obj, FIELD_FRAME_ID)
    ts = _require_int(obj, FIELD_TS)

    motion = _require_bool(obj, FIELD_MOTION)
    camera_ok = _require_bool(obj, FIELD_CAMERA_OK)

    raw_bbox = obj.get(FIELD_BBOX)
    bbox = _bbox_from_list(raw_bbox) if raw_bbox is not None else None

    area = _parse_float_default(obj.get(FIELD_AREA), FIELD_AREA, 0.0)

    last_error = obj.get(FIELD_LAST_ERROR)
    if last_error is not None:
        last_error = str(last_error)

    # Present-but-null (a peer sending {"version": null}) coerces to "unknown"
    # too — .get's default only fires on an ABSENT key, so a present null would
    # otherwise slip through and violate the declared `version: str` type.
    version = obj.get(FIELD_VERSION)
    version = "unknown" if version is None else str(version)

    system = obj.get(FIELD_SYSTEM)
    if system is not None and not isinstance(system, dict):
        raise WireParseError(f"{FIELD_SYSTEM!r} must be an object or null, got {type(system).__name__}")

    return StatusSnapshot(
        frame_id=frame_id,
        ts=ts,
        motion=motion,
        bbox=bbox,
        area=area,
        camera_ok=camera_ok,
        last_error=last_error,
        version=version,
        system=system,
    )


def _require_int(source: dict, key: str) -> int:
    """Fetch a required integer field; raise ``WireParseError`` if absent/non-int.

    Works for both a lower-cased header dict and a status JSON dict — the header
    parsers pass the canonical header name and the dict is already lower-cased,
    so we look the key up case-insensitively.

    Strictly integer: a stream-header value is a numeric string (parsed), a status
    value is already a real ``int``. A ``bool`` (an ``int`` subclass) and a
    ``float`` are REJECTED rather than coerced — ``frame_id`` is the identity /
    ordering key, so silently truncating ``2.9`` → ``2`` (or ``True`` → ``1``)
    could merge or misorder distinct frames instead of surfacing corruption.
    """
    if key.lower() in source:
        raw = source[key.lower()]
    elif key in source:
        raw = source[key]
    else:
        raise WireParseError(f"missing required field {key!r}")
    if isinstance(raw, bool):
        raise WireParseError(f"{key!r} must be an integer, not a bool")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise WireParseError(f"{key!r} is not an integer: {raw!r}") from exc
    raise WireParseError(f"{key!r} must be an integer, got {type(raw).__name__}")


def _require_bool(source: dict, key: str) -> bool:
    """Fetch a required JSON boolean; raise ``WireParseError`` if absent/non-bool.

    Deliberately strict — a bare ``bool(...)`` would accept any truthy value, so a
    stray ``"false"`` / ``"0"`` string from a non-conforming peer would read as
    ``True`` and invert the very motion / camera-health verdict ``/status`` is
    meant to be authoritative for. A conforming edge always sends a real JSON
    boolean, so requiring one costs nothing and fails loud on corruption.
    """
    if key not in source:
        raise WireParseError(f"missing required field {key!r}")
    value = source[key]
    if not isinstance(value, bool):
        raise WireParseError(f"{key!r} must be a boolean, got {type(value).__name__}")
    return value


def _require_motion(headers: dict) -> bool:
    """Parse the required ``X-Motion`` header: ``"1"`` → True, ``"0"`` → False."""
    raw = headers.get(HEADER_MOTION.lower())
    if raw is None:
        raise WireParseError(f"missing required header {HEADER_MOTION!r}")
    if raw == "1":
        return True
    if raw == "0":
        return False
    raise WireParseError(f"{HEADER_MOTION!r} must be '1' or '0', got {raw!r}")


def _parse_float_default(raw: "str | float | None", name: str, default: float) -> float:
    """Float-parse an optional field: ``None`` → default, garbage → error."""
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise WireParseError(f"{name!r} is not a number: {raw!r}") from exc


def _bbox_from_list(raw: object) -> "tuple[float, float, float, float]":
    """Convert a JSON ``bbox`` list to a 4-float tuple; raise on a bad shape."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise WireParseError(f"{FIELD_BBOX!r} must be a 4-element list, got {raw!r}")
    try:
        x, y, w, h = (float(v) for v in raw)
    except (TypeError, ValueError) as exc:
        raise WireParseError(f"{FIELD_BBOX!r} has a non-numeric component: {raw!r}") from exc
    return (x, y, w, h)

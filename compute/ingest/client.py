"""The compute-side client for the Pi edge's data plane (``/stream`` + ``/status``).

This is the client half of the edge↔compute split: the Pi is a pure server that
only ever listens, so *this* code initiates every connection (see
``docs/ARCHITECTURE.md`` — "Communication and data flow"). It exists to carry the
two signals the Pi puts on the wire — the continuous MJPEG stream and the pulled
``/status`` health snapshot — into the compute tier as typed values, and to make
the Pi's liveness observable from the client's own connections.

Why a manual multipart parse instead of ``cv2.VideoCapture(url)`` (which
``ARCHITECTURE.md`` name-checks): ``VideoCapture`` hands back *decoded frames only*
and discards the multipart part headers, losing ``X-Frame-Id``/``X-Timestamp``
(frame identity) and ``X-Motion`` (inline motion) — the very pull-signals this
client exists to carry. So we open a streamed ``requests`` GET, frame each body by
the shared wire definition, and ``cv2.imdecode`` the JPEG *lazily*, only when the
caller actually touches ``StreamFrame.image``. A frame the consumer skips on the
motion signal therefore costs zero decode, and ``cv2``/``numpy`` are imported only
inside that path — so this module's parsing and liveness logic import and test
cleanly without the CV stack installed.

Liveness model (one-way, per the architecture): ``/status.camera_ok`` is the
authoritative camera-health oracle; a failed ``/status`` request or a dropped
stream connection means the Pi/network is down and surfaces as
``EdgeUnavailable``. A stream *stall* — no new part within the read timeout — is a
reconnect trigger, not a health verdict on its own; the reconnecting wrapper
cannot tell *why* a stream dropped (Pi down, network down, or a wedged camera on a
healthy Pi that still answers ``/status`` with ``camera_ok=false``), so a consumer
that must know the camera died has to poll ``get_status()`` regardless.
"""
from __future__ import annotations

import io
import os
import random
import time
from typing import Iterator

import requests
import urllib3

from shared.wire import (
    BOUNDARY,
    StatusSnapshot,
    StreamFrameMeta,
    WireParseError,
    parse_part_headers,
    parse_status,
)

# The env var the constructor falls back to when no base_url is passed. The
# compute tier has no config store yet, so this is the one place a Pi address is
# configured out-of-band (e.g. from a systemd unit / .env).
_ENV_BASE_URL = "CAT_PI_URL"

# Default (connect, read) timeouts in seconds. The READ timeout doubles as the
# stream STALL threshold: if no new bytes arrive within it, the stream is
# considered stalled and reconnected. Both are constructor-overridable.
_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_TIMEOUT = 5.0

# Reconnect backoff bounds for iter_stream_reconnecting (seconds).
_DEFAULT_INITIAL_BACKOFF = 0.5
_DEFAULT_MAX_BACKOFF = 10.0

# The "--<boundary>" part separator the edge writes before every part; its bytes
# come from the shared constant so this consumer and the edge serializer agree.
_BOUNDARY_LINE = ("--" + BOUNDARY).encode("latin-1")

# Errors that mean "the connection/transport failed" while opening or reading the
# stream — all funnelled into EdgeUnavailable so the caller sees one liveness type.
# socket.timeout is an OSError subclass; urllib3's ReadTimeoutError/ProtocolError
# are HTTPError subclasses; requests wraps most of these in RequestException.
_TRANSPORT_ERRORS = (
    requests.exceptions.RequestException,
    urllib3.exceptions.HTTPError,
    OSError,
)


class EdgeUnavailable(Exception):
    """The Pi edge could not be reached, or an open stream dropped/stalled.

    This is the client's liveness signal: raised when a request fails to connect,
    a stream connection drops, a stream stalls past the read timeout, ``/status``
    returns non-200, or the stream bytes are corrupt (a ``WireParseError`` is
    treated like a stall — drop and reconnect rather than emit a half-parsed
    frame). It does NOT distinguish *why* the edge is unreachable; camera health
    specifically comes from ``StatusSnapshot.camera_ok``, not from this exception.
    """


class StreamFrame:
    """One frame off ``/stream``: its metadata plus the raw (undecoded) JPEG body.

    JPEG decoding is deferred to first access of ``.image`` (then cached), because
    a consumer that skips this frame on the motion signal should pay nothing for a
    decode it never uses. ``cv2``/``numpy`` are imported inside ``.image`` for the
    same reason the whole client avoids them at import time — so parsing/liveness
    stay usable and testable without the CV stack.
    """

    __slots__ = ("meta", "jpeg", "_image", "_decoded")

    def __init__(self, meta: StreamFrameMeta, jpeg: bytes) -> None:
        self.meta = meta
        self.jpeg = jpeg
        self._image = None
        self._decoded = False  # distinguishes "not yet decoded" from a cached None

    @property
    def image(self):
        """The decoded BGR ndarray, ``cv2.imdecode``'d on first access and cached.

        Imports ``cv2``/``numpy`` lazily here (not at module load). Raises
        ``ValueError`` if the JPEG body can't be decoded — corrupt image bytes are
        a data problem for the caller to handle, not a transport/liveness one.
        """
        if not self._decoded:
            import cv2
            import numpy as np

            arr = np.frombuffer(self.jpeg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("failed to decode JPEG frame body")
            self._image = img
            self._decoded = True
        return self._image


class EdgeClient:
    """Connects to one Pi edge's ``/stream`` and ``/status``.

    ``base_url`` comes from the constructor argument, else the ``CAT_PI_URL`` env
    var; if NEITHER is set the constructor raises a ``ValueError`` immediately —
    surfacing the misconfiguration at construction rather than deferring a
    ``None``-into-URL crash to first use.

    Concurrency: one ``iter_stream()`` and one ``get_status()`` may run at once
    from different threads. Each call issues its own independent HTTP request (no
    shared ``Session`` whose connection pool could couple them), and
    ``get_status()`` never touches the open stream — so the health poll keeps
    answering while the long-lived stream is held open.
    """

    def __init__(
        self,
        base_url: "str | None" = None,
        *,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ) -> None:
        resolved = base_url if base_url is not None else os.environ.get(_ENV_BASE_URL)
        if not resolved:
            raise ValueError(
                "EdgeClient needs a base URL: pass base_url=... or set the "
                f"{_ENV_BASE_URL} environment variable (e.g. http://cat-pi.local:8000)"
            )
        # Normalize once so route joins are a plain f-string and a trailing slash
        # in config can't produce a double slash.
        self._base_url = resolved.rstrip("/")
        self._connect_timeout = connect_timeout
        # The read timeout is also the stream stall threshold (see module docstring).
        self._read_timeout = read_timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def iter_stream(self) -> "Iterator[StreamFrame]":
        """Yield ``StreamFrame``s off a single long-lived ``GET /stream``.

        Opens the stream with ``stream=True`` and a ``(connect, read)`` timeout
        whose read component is the stall threshold. Wraps ``resp.raw`` in a
        buffered reader, then per part: reads the boundary + header lines through
        the blank line, hands that block to ``parse_part_headers`` →
        ``(meta, content_length)``, reads exactly ``content_length`` body bytes
        (Content-Length framing — robust; the edge always sends it), and yields
        ``StreamFrame(meta, jpeg)``.

        Any transport failure (connect error, dropped connection, or a stall past
        the read timeout) or stream corruption (a ``WireParseError``) raises
        ``EdgeUnavailable``. The generator is a plain blocking iterator; the
        caller owns threading and reconnection (or uses
        ``iter_stream_reconnecting``).
        """
        url = f"{self._base_url}/stream"
        try:
            resp = requests.get(
                url,
                stream=True,
                timeout=(self._connect_timeout, self._read_timeout),
            )
        except _TRANSPORT_ERRORS as exc:
            raise EdgeUnavailable(f"failed to open stream {url!r}: {exc}") from exc

        try:
            if resp.status_code != 200:
                raise EdgeUnavailable(
                    f"stream {url!r} returned HTTP {resp.status_code}"
                )
            # resp.raw is a urllib3 HTTPResponse (an io.IOBase that de-chunks on
            # read); BufferedReader gives us readline()/read() with buffering so a
            # single JPEG body can span many socket reads transparently.
            reader = io.BufferedReader(resp.raw)
            while True:
                block = _read_header_block(reader)
                if block is None:
                    # Clean EOF at a part boundary: the edge closed the stream.
                    raise EdgeUnavailable(f"stream {url!r} closed by edge")
                meta, content_length = parse_part_headers(block)
                jpeg = _read_exact(reader, content_length)
                yield StreamFrame(meta, jpeg)
        except EdgeUnavailable:
            raise
        except _TRANSPORT_ERRORS as exc:
            raise EdgeUnavailable(f"stream {url!r} dropped or stalled: {exc}") from exc
        except Exception as exc:
            # A WireParseError (corrupt part header / bbox) — and any other parse
            # slip — is treated like a stall: drop the connection so the caller
            # reconnects rather than surfacing a half-parsed frame.
            raise EdgeUnavailable(f"stream {url!r} corrupt: {exc}") from exc
        finally:
            # Always release the connection, whether we finished, errored, or the
            # consumer stopped iterating (GeneratorExit runs this finally).
            resp.close()

    def get_status(self) -> StatusSnapshot:
        """Fetch and parse ``GET /status`` — the health/liveness oracle.

        Returns a ``StatusSnapshot`` (motion + camera-health + host metrics).
        Raises ``EdgeUnavailable`` on a connection error or a non-200 response.
        The caller drives the poll cadence; there is no internal timer, and this
        call never touches an open stream.
        """
        url = f"{self._base_url}/status"
        try:
            resp = requests.get(
                url, timeout=(self._connect_timeout, self._read_timeout)
            )
        except _TRANSPORT_ERRORS as exc:
            raise EdgeUnavailable(f"failed to poll {url!r}: {exc}") from exc
        try:
            if resp.status_code != 200:
                raise EdgeUnavailable(f"{url!r} returned HTTP {resp.status_code}")
            try:
                obj = resp.json()
            except ValueError as exc:
                raise EdgeUnavailable(f"{url!r} returned a non-JSON body") from exc
        finally:
            resp.close()
        # parse_status may raise WireParseError on a malformed payload; that is a
        # contract violation, not a liveness event, so it propagates unwrapped.
        return parse_status(obj)

    def iter_stream_reconnecting(
        self,
        *,
        initial_backoff: float = _DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
    ) -> "Iterator[StreamFrame]":
        """Endlessly yield frames, re-opening the stream on ``EdgeUnavailable``.

        A thin convenience for callers that just want an unbroken frame feed and
        don't need to observe each drop. Backs off exponentially between attempts
        (``initial_backoff`` → ``max_backoff`` cap) with jitter to avoid a
        thundering-herd retry, and resets the backoff after any successful frame so
        a long healthy run followed by a drop retries promptly.

        It cannot tell *why* the stream dropped, so a consumer that needs camera
        health must still poll ``get_status()`` (see the module docstring).
        """
        backoff = initial_backoff
        while True:
            try:
                for frame in self.iter_stream():
                    backoff = initial_backoff  # healthy again — reset the backoff
                    yield frame
            except EdgeUnavailable:
                pass  # fall through to the backoff sleep and re-open
            # Full jitter: sleep a random slice of the current window, then grow it
            # toward the cap. Python's random is fine here (no crypto need); this
            # only spaces out reconnects.
            time.sleep(random.uniform(0.0, backoff))
            backoff = min(backoff * 2.0, max_backoff)


def _read_header_block(reader: "io.BufferedReader") -> "bytes | None":
    """Read one part's header block: boundary + headers through the blank line.

    Skips any leading blank lines first — after a part body the edge writes a
    trailing ``CRLF``, which shows up as a blank line before the next ``--frame``
    separator; skipping it here means the body reader doesn't have to consume that
    trailing terminator explicitly. Returns the joined block bytes (exactly what
    ``format_part_headers`` produced, so ``parse_part_headers`` inverts it), or
    ``None`` on a clean EOF at a boundary (the stream ended).
    """
    lines: "list[bytes]" = []
    # Skip blank lines that precede the boundary (the previous body's trailing
    # CRLF, and defensively any stray blanks), then require the "--<boundary>"
    # separator — matched against the shared constant so the consumer is coupled to
    # the same boundary the edge serializes, and a misframed stream is caught here
    # rather than silently accepted.
    while True:
        line = reader.readline()
        if line == b"":
            return None  # EOF with nothing pending → clean stream end
        if line in (b"\r\n", b"\n"):
            continue  # trailing CRLF from the previous body / a stray blank
        if line.rstrip(b"\r\n") != _BOUNDARY_LINE:
            # Where a part separator was expected, anything else is corruption;
            # iter_stream turns this WireParseError into EdgeUnavailable (reconnect).
            raise WireParseError(f"expected multipart boundary, got {line!r}")
        break  # the "--<boundary>" separator line
    lines.append(line)
    # Accumulate header lines up to and including the terminating blank line.
    while True:
        line = reader.readline()
        if line == b"":
            # EOF mid-headers: truncated part. Give parse_part_headers what we have
            # so it raises WireParseError (→ treated as a stall by iter_stream).
            break
        lines.append(line)
        if line in (b"\r\n", b"\n"):
            break
    return b"".join(lines)


def _read_exact(reader: "io.BufferedReader", n: int) -> bytes:
    """Read exactly ``n`` bytes, or raise if the stream ends early.

    ``BufferedReader.read(n)`` can return fewer than ``n`` bytes, so we loop; a
    short read at EOF means the body was truncated (a dropped connection), which
    surfaces as ``EdgeUnavailable`` via ``iter_stream``'s handler.
    """
    chunks: "list[bytes]" = []
    remaining = n
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            raise EdgeUnavailable(
                f"stream ended mid-frame: wanted {n} body bytes, got {n - remaining}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

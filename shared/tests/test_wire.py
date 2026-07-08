"""Tests for the edge↔compute wire contract (shared/wire.py).

The whole point of shared/wire.py is that the edge serializes and the compute
tier parses through ONE definition, so these tests guard that definition:

- The round-trip invariant (``format_part_headers`` → ``parse_part_headers`` ==
  identity) is what actually prevents edge↔compute drift, exercised for both a
  motion-active part (with bbox) and a motion-inactive part (no bbox).
- A byte-exactness check pins the literal wire layout, so an accidental reorder
  or reformat of a header can't slip through while still round-tripping.
- ``parse_status`` is checked on the happy path, on missing-field defaults (an
  older edge), and on the bbox list→tuple conversion that makes the two
  snapshots share a bbox type.
- Malformed required fields must raise ``WireParseError`` (corruption, not a
  missing field), and unknown extra headers/fields must be ignored (so an
  additive contract change never breaks an old peer).

No cv2/requests/Flask here — the contract is pure, so the tests are too.
"""
from __future__ import annotations

import pytest

from shared import wire
from shared.wire import (
    BOUNDARY,
    StatusSnapshot,
    StreamFrameMeta,
    WireParseError,
    format_part_headers,
    parse_part_headers,
    parse_status,
)


# --- Round-trip invariant ---------------------------------------------------


def test_roundtrip_motion_active_with_bbox():
    # A motion-active part carries a bbox; format → parse must recover it exactly.
    meta = StreamFrameMeta(
        frame_id=42,
        ts=1_700_000_000_123,
        motion=True,
        bbox=(0.1, 0.2, 0.3, 0.4),
        area=0.0125,
    )
    block = format_part_headers(meta, content_length=2048)
    parsed_meta, content_length = parse_part_headers(block)
    assert parsed_meta == meta
    assert content_length == 2048


def test_roundtrip_motion_inactive_no_bbox():
    # Motion inactive → no X-Bbox on the wire, so bbox parses back to None, but
    # X-Area is still emitted (the intended contract change) and round-trips.
    meta = StreamFrameMeta(
        frame_id=7,
        ts=1_700_000_000_000,
        motion=False,
        bbox=None,
        area=0.0,
    )
    block = format_part_headers(meta, content_length=1024)
    parsed_meta, content_length = parse_part_headers(block)
    assert parsed_meta == meta
    assert content_length == 1024


# --- Byte-exactness ---------------------------------------------------------


def test_format_part_headers_byte_exact_motion_active():
    # Locks the literal layout for the motion-active case: this is byte-for-byte
    # what edge/server/app.py::_build_part emitted historically (with X-Bbox
    # BEFORE X-Area). If this changes, the edge and any old client diverge.
    meta = StreamFrameMeta(
        frame_id=3,
        ts=1234567890,
        motion=True,
        bbox=(0.5, 0.25, 0.125, 0.0625),
        area=0.03,
    )
    block = format_part_headers(meta, content_length=999)
    expected = (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: 999\r\n"
        b"X-Frame-Id: 3\r\n"
        b"X-Timestamp: 1234567890\r\n"
        b"X-Motion: 1\r\n"
        b"X-Bbox: 0.5,0.25,0.125,0.0625\r\n"
        b"X-Area: 0.03\r\n"
        b"\r\n"
    )
    assert block == expected


def test_format_part_headers_byte_exact_motion_inactive():
    # The one intended change vs. history: X-Area is emitted even when idle (no
    # X-Bbox, X-Motion: 0). Header block ends in the CRLF CRLF blank line.
    meta = StreamFrameMeta(
        frame_id=8,
        ts=42,
        motion=False,
        bbox=None,
        area=0.0,
    )
    block = format_part_headers(meta, content_length=17)
    expected = (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: 17\r\n"
        b"X-Frame-Id: 8\r\n"
        b"X-Timestamp: 42\r\n"
        b"X-Motion: 0\r\n"
        b"X-Area: 0.0\r\n"
        b"\r\n"
    )
    assert block == expected


def test_format_part_headers_ends_with_blank_line():
    # The block is headers-only through the terminating blank line; the JPEG body
    # is the caller's to append. Ending in CRLF CRLF is what separates the two.
    meta = StreamFrameMeta(frame_id=1, ts=1, motion=False, bbox=None, area=0.0)
    block = format_part_headers(meta, content_length=10)
    assert block.endswith(b"\r\n\r\n")


# --- parse_part_headers robustness ------------------------------------------


def test_parse_part_headers_order_independent_and_ignores_unknown():
    # Parsing is by header name, not position; unknown headers (a future additive
    # change) are ignored rather than fatal.
    block = (
        b"--" + BOUNDARY.encode() + b"\r\n"
        b"X-Future-Thing: whatever\r\n"
        b"X-Area: 0.9\r\n"
        b"X-Motion: 1\r\n"
        b"X-Bbox: 0.0,0.0,1.0,1.0\r\n"
        b"X-Timestamp: 55\r\n"
        b"X-Frame-Id: 99\r\n"
        b"Content-Length: 500\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"\r\n"
    )
    meta, content_length = parse_part_headers(block)
    assert meta == StreamFrameMeta(
        frame_id=99, ts=55, motion=True, bbox=(0.0, 0.0, 1.0, 1.0), area=0.9
    )
    assert content_length == 500


def test_parse_part_headers_missing_area_defaults_zero():
    # A pre-"X-Area-always" edge omits X-Area when idle; it must still parse, with
    # area defaulting to 0.0 and bbox to None.
    block = (
        b"--frame\r\n"
        b"Content-Length: 12\r\n"
        b"X-Frame-Id: 5\r\n"
        b"X-Timestamp: 6\r\n"
        b"X-Motion: 0\r\n"
        b"\r\n"
    )
    meta, content_length = parse_part_headers(block)
    assert meta == StreamFrameMeta(
        frame_id=5, ts=6, motion=False, bbox=None, area=0.0
    )
    assert content_length == 12


@pytest.mark.parametrize(
    "block",
    [
        # non-integer X-Frame-Id
        (
            b"--frame\r\n"
            b"Content-Length: 1\r\n"
            b"X-Frame-Id: notanint\r\n"
            b"X-Timestamp: 6\r\n"
            b"X-Motion: 0\r\n"
            b"\r\n"
        ),
        # absent X-Motion (required)
        (
            b"--frame\r\n"
            b"Content-Length: 1\r\n"
            b"X-Frame-Id: 5\r\n"
            b"X-Timestamp: 6\r\n"
            b"\r\n"
        ),
        # un-parseable X-Bbox (wrong component count)
        (
            b"--frame\r\n"
            b"Content-Length: 1\r\n"
            b"X-Frame-Id: 5\r\n"
            b"X-Timestamp: 6\r\n"
            b"X-Motion: 1\r\n"
            b"X-Bbox: 0.1,0.2,0.3\r\n"
            b"\r\n"
        ),
        # X-Bbox with a non-numeric component
        (
            b"--frame\r\n"
            b"Content-Length: 1\r\n"
            b"X-Frame-Id: 5\r\n"
            b"X-Timestamp: 6\r\n"
            b"X-Motion: 1\r\n"
            b"X-Bbox: 0.1,0.2,x,0.4\r\n"
            b"\r\n"
        ),
        # X-Motion neither "0" nor "1"
        (
            b"--frame\r\n"
            b"Content-Length: 1\r\n"
            b"X-Frame-Id: 5\r\n"
            b"X-Timestamp: 6\r\n"
            b"X-Motion: yes\r\n"
            b"\r\n"
        ),
        # missing Content-Length (required for body framing)
        (
            b"--frame\r\n"
            b"X-Frame-Id: 5\r\n"
            b"X-Timestamp: 6\r\n"
            b"X-Motion: 0\r\n"
            b"\r\n"
        ),
    ],
)
def test_parse_part_headers_malformed_raises(block):
    with pytest.raises(WireParseError):
        parse_part_headers(block)


# --- parse_status -----------------------------------------------------------


def test_parse_status_happy_path_converts_bbox_to_tuple():
    obj = {
        "frame_id": 12,
        "ts": 1_700_000_000_999,
        "motion": True,
        "bbox": [0.1, 0.2, 0.3, 0.4],  # JSON list
        "area": 0.05,
        "camera_ok": True,
        "last_error": None,
        "version": "v0.1.0",
        "system": {"cpu_percent": 3.2, "mem_percent": 40.0},
    }
    snap = parse_status(obj)
    assert snap == StatusSnapshot(
        frame_id=12,
        ts=1_700_000_000_999,
        motion=True,
        bbox=(0.1, 0.2, 0.3, 0.4),  # now a tuple, matching StreamFrameMeta
        area=0.05,
        camera_ok=True,
        last_error=None,
        version="v0.1.0",
        system={"cpu_percent": 3.2, "mem_percent": 40.0},
    )
    assert isinstance(snap.bbox, tuple)


def test_parse_status_missing_fields_default():
    # An older edge (pre-system, pre-version, idle so no bbox/area). Only the four
    # required fields are present; the rest fall back to neutral defaults.
    obj = {
        "frame_id": 0,
        "ts": 0,
        "motion": False,
        "camera_ok": False,
    }
    snap = parse_status(obj)
    assert snap == StatusSnapshot(
        frame_id=0,
        ts=0,
        motion=False,
        bbox=None,
        area=0.0,
        camera_ok=False,
        last_error=None,
        version="unknown",
        system=None,
    )


def test_parse_status_ignores_unknown_fields():
    obj = {
        "frame_id": 1,
        "ts": 2,
        "motion": False,
        "camera_ok": True,
        "some_future_field": {"nested": 1},
    }
    snap = parse_status(obj)
    assert snap.frame_id == 1
    assert snap.camera_ok is True


def test_parse_status_carries_last_error_string():
    obj = {
        "frame_id": 3,
        "ts": 4,
        "motion": False,
        "camera_ok": False,
        "last_error": "cannot open device",
    }
    snap = parse_status(obj)
    assert snap.camera_ok is False
    assert snap.last_error == "cannot open device"


@pytest.mark.parametrize(
    "obj",
    [
        # missing required frame_id
        {"ts": 1, "motion": False, "camera_ok": True},
        # missing required ts
        {"frame_id": 1, "motion": False, "camera_ok": True},
        # missing required motion
        {"frame_id": 1, "ts": 2, "camera_ok": True},
        # missing required camera_ok
        {"frame_id": 1, "ts": 2, "motion": False},
        # non-integer frame_id
        {"frame_id": "nope", "ts": 2, "motion": False, "camera_ok": True},
        # bbox with wrong shape
        {"frame_id": 1, "ts": 2, "motion": True, "camera_ok": True, "bbox": [0.1, 0.2]},
        # bbox with a non-numeric component
        {"frame_id": 1, "ts": 2, "motion": True, "camera_ok": True, "bbox": [0.1, 0.2, "x", 0.4]},
        # system present but not an object
        {"frame_id": 1, "ts": 2, "motion": False, "camera_ok": True, "system": "busy"},
        # area present but non-numeric
        {"frame_id": 1, "ts": 2, "motion": False, "camera_ok": True, "area": "lots"},
    ],
)
def test_parse_status_malformed_raises(obj):
    with pytest.raises(WireParseError):
        parse_status(obj)


# --- The wire change matches the live edge serializer -----------------------


def test_format_part_headers_matches_edge_build_part_motion_active():
    # Guard the "byte-for-byte identical to the edge for the motion-active case"
    # claim directly against the edge's serializer, so this test fails loudly if
    # either side is edited without the other. Reconstruct _build_part's header
    # block from the same inputs (its header block is `part` up to the blank line).
    bx, by, bw, bh = (0.5, 0.25, 0.125, 0.0625)
    area = 0.03
    frame_id, ts, data_len = 3, 1234567890, 999
    edge_header_block = (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(data_len).encode() + b"\r\n"
        b"X-Frame-Id: " + str(frame_id).encode() + b"\r\n"
        b"X-Timestamp: " + str(ts).encode() + b"\r\n"
        b"X-Motion: 1\r\n"
        b"X-Bbox: " + f"{bx},{by},{bw},{bh}".encode() + b"\r\n"
        b"X-Area: " + str(area).encode() + b"\r\n"
        b"\r\n"
    )
    meta = StreamFrameMeta(
        frame_id=frame_id, ts=ts, motion=True, bbox=(bx, by, bw, bh), area=area
    )
    assert format_part_headers(meta, content_length=data_len) == edge_header_block


def test_module_exposes_boundary_constant():
    # Both the mimetype and the "--<token>" separator must source this one value.
    assert wire.BOUNDARY == "frame"

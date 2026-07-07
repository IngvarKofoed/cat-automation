"""Tests for the edge Flask app (edge/server/app.py), against FakeCaptureSource.

No real camera or settings.json is touched: CAT_EDGE_CONFIG is pointed at a
tmp file for every test. See docs/specs/2026-07-07-edge-stills-mvp.md.
"""
from __future__ import annotations

import json

import pytest

from edge.capture.base import CaptureError, CaptureSource
from edge.capture.fake_source import FakeCaptureSource
from edge.config import settings
from edge.server.app import create_app


class FailingCaptureSource(CaptureSource):
    """A capture source whose read() always fails, simulating a bad device."""

    def __init__(self, device: "int | str" = 0) -> None:
        del device

    def read(self):
        raise CaptureError("cannot open device")

    def close(self) -> None:
        pass


def _factory_with_bad_device(bad_device: "int | str"):
    """A source_factory where `bad_device` yields a source that fails to read.

    Any other device yields a working FakeCaptureSource.
    """

    def factory(device: "int | str") -> CaptureSource:
        if device == bad_device:
            return FailingCaptureSource(device)
        return FakeCaptureSource(device)

    return factory


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Point CAT_EDGE_CONFIG at a tmp file so no real settings.json is touched."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(path))
    return path


@pytest.fixture
def client(config_path):
    """A test client for an app wired to the fake capture source."""
    app = create_app(source_factory=FakeCaptureSource)
    return app.test_client()


# --- /frame ---


def test_frame_returns_jpeg(client):
    resp = client.get("/frame")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"
    assert resp.data[:2] == b"\xff\xd8"


# --- GET /api/config ---


def test_get_config_returns_default_device(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 0}


# --- POST /api/config: valid device ---


def test_post_config_valid_device_updates_and_persists(client, config_path):
    resp = client.post("/api/config", json={"device": 1})
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 1}

    resp = client.get("/api/config")
    assert resp.get_json() == {"device": 1}

    assert settings.load_settings()["device"] == 1
    saved = json.loads(config_path.read_text())
    assert saved["device"] == 1


# --- POST /api/config: invalid device ---


def test_post_config_missing_device_is_400(client):
    resp = client.post("/api/config", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_config_none_device_is_400(client):
    resp = client.post("/api/config", json={"device": None})
    assert resp.status_code == 400


def test_post_config_wrong_type_device_is_400(client):
    resp = client.post("/api/config", json={"device": 3.14})
    assert resp.status_code == 400


def test_post_config_non_object_body_is_400(client):
    # A valid-but-non-object JSON body must be a clean 400, not a 500.
    resp = client.post("/api/config", data="5", content_type="application/json")
    assert resp.status_code == 400


# --- POST /api/config: candidate source fails to open ---


def test_post_config_source_open_failure_keeps_previous(config_path):
    app = create_app(source_factory=_factory_with_bad_device(bad_device=99))
    client = app.test_client()

    resp = client.post("/api/config", json={"device": 99})
    assert 400 <= resp.status_code < 500

    # Previous source/device is kept.
    resp = client.get("/api/config")
    assert resp.get_json() == {"device": 0}

    # Nothing was ever persisted for the failed switch.
    assert not config_path.exists()


# --- /api/cameras ---


def test_cameras_lists_structural_entries(client):
    resp = client.get("/api/cameras")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "cameras" in body
    assert isinstance(body["cameras"], list)
    for entry in body["cameras"]:
        assert "device" in entry
        assert "label" in entry


# --- / (config UI) ---


def test_index_serves_html_with_capture_button(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Capture" in resp.data


# --- settings.py, tested directly ---


def test_load_settings_missing_file_returns_defaults(config_path):
    assert settings.load_settings() == settings.DEFAULTS


def test_load_settings_corrupt_file_returns_defaults(config_path):
    config_path.write_text("not valid json {{{")
    assert settings.load_settings() == settings.DEFAULTS


def test_load_settings_non_dict_json_returns_defaults(config_path):
    # Valid JSON but not an object must fall back to defaults, not crash callers.
    config_path.write_text("null")
    assert settings.load_settings() == settings.DEFAULTS
    config_path.write_text("[1, 2, 3]")
    assert settings.load_settings() == settings.DEFAULTS


def test_create_app_bad_persisted_device_does_not_crash(config_path):
    # A hand-edited, parseable-but-invalid device must not wedge startup.
    config_path.write_text(json.dumps({"device": None}))
    app = create_app(source_factory=FakeCaptureSource)  # must not raise
    resp = app.test_client().get("/api/config")
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 0}  # fell back to the default


def test_save_settings_then_load_settings_roundtrips(config_path):
    settings.save_settings({"device": "/dev/video0"})
    assert settings.load_settings() == {"device": "/dev/video0"}

"""Tests for ``EdgeClient.get_config()`` (compute/ingest/client.py).

Mirrors the ``get_status`` unit tests in ``test_ingest.py``: a fake ``requests``
response stands in for the Pi's ``GET /api/config`` so these run with no camera,
no GPU, and no real edge process. ``get_config`` has no ``shared.wire`` parser
(config is a Pi-owned settings blob, not a data-plane wire contract), so the
JSON body is returned as a plain ``dict`` rather than a typed snapshot.
"""
from __future__ import annotations

import pytest
import requests

from compute.ingest import EdgeClient, EdgeUnavailable


class _FakeResponse:
    """A stand-in for a non-streamed ``requests`` response (status/config)."""

    def __init__(self, status_code: int = 200, json_obj=None, raise_on_json: bool = False) -> None:
        self.status_code = status_code
        self._json_obj = json_obj
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("no JSON body")
        return self._json_obj

    def close(self) -> None:
        pass


def test_get_config_parses_json(monkeypatch):
    payload = {
        "device": 0,
        "rotation": 90,
        "clip": [0.0, 0.0, 1.0, 1.0],
        "fps": 5,
        "focus": None,
        "var_threshold": 16.0,
        "learning_rate": 0.01,
        "min_area": 0.001,
        "max_area_fraction": 0.5,
        "persistence": 2,
        "motion_downscale": 320,
    }

    def fake_get(url, **kwargs):
        assert url.endswith("/api/config")
        return _FakeResponse(status_code=200, json_obj=payload)

    monkeypatch.setattr(requests, "get", fake_get)
    client = EdgeClient(base_url="http://pi.test:8000")
    cfg = client.get_config()

    assert cfg == payload
    assert cfg["var_threshold"] == 16.0
    assert cfg["learning_rate"] == 0.01
    assert cfg["min_area"] == 0.001
    assert cfg["max_area_fraction"] == 0.5
    assert cfg["persistence"] == 2
    assert cfg["motion_downscale"] == 320


def test_get_config_connection_error_raises_unavailable(monkeypatch):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        client.get_config()


def test_get_config_non_200_raises_unavailable(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, **kw: _FakeResponse(status_code=500)
    )
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        client.get_config()


def test_get_config_non_json_body_raises_unavailable(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kw: _FakeResponse(status_code=200, raise_on_json=True),
    )
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        client.get_config()


def test_get_config_non_object_body_raises_unavailable(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kw: _FakeResponse(status_code=200, json_obj=[1, 2, 3]),
    )
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        client.get_config()

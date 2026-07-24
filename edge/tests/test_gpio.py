"""Tests for the manual GPIO output driver and its edge endpoints.

No real GPIO is touched: a fake ``PinBackend`` records writes, and the app is
wired to a ``GpioOutputs`` built on it. See edge/actuators/gpio.py.
"""
from __future__ import annotations

import pytest

from edge.actuators.gpio import GPIO_OUTPUTS, GpioOutputs, GpioUnavailable
from edge.capture.fake_source import FakeCaptureSource
from edge.server.app import create_app


class FakeBackend:
    """Records every write so a test can assert the pin levels commanded."""

    def __init__(self, pins: "list[int]") -> None:
        self.pins = list(pins)
        self.writes: "list[tuple[int, bool]]" = []
        self.levels = {pin: False for pin in pins}
        self.closed = False

    def write(self, pin: int, high: bool) -> None:
        self.writes.append((pin, high))
        self.levels[pin] = high

    def close(self) -> None:
        self.closed = True


def _raising_factory(pins):
    raise RuntimeError("no gpiochip")


# --- GpioOutputs unit ---


def test_defaults_low_and_available_with_backend():
    backend = FakeBackend([o["pin"] for o in GPIO_OUTPUTS])
    gpio = GpioOutputs(backend_factory=lambda pins: backend)
    assert gpio.available is True
    assert gpio.names() == ["light", "aux"]
    outputs = gpio.outputs()
    assert [o["high"] for o in outputs] == [False, False]
    assert outputs[0] == {"name": "light", "pin": 27, "label": "Light", "high": False}


def test_set_high_then_low_drives_pin_and_tracks_state():
    backend = FakeBackend([o["pin"] for o in GPIO_OUTPUTS])
    gpio = GpioOutputs(backend_factory=lambda pins: backend)

    gpio.set("light", True)
    assert backend.writes[-1] == (27, True)
    assert {o["name"]: o["high"] for o in gpio.outputs()}["light"] is True

    gpio.set("light", False)
    assert backend.writes[-1] == (27, False)
    assert {o["name"]: o["high"] for o in gpio.outputs()}["light"] is False
    # The spare channel is independent and untouched.
    assert {o["name"]: o["high"] for o in gpio.outputs()}["aux"] is False


def test_set_unknown_name_raises_keyerror():
    gpio = GpioOutputs(backend_factory=lambda pins: FakeBackend(pins))
    with pytest.raises(KeyError):
        gpio.set("nonexistent", True)


def test_unavailable_when_backend_fails_to_build():
    gpio = GpioOutputs(backend_factory=_raising_factory)
    assert gpio.available is False
    # State still readable (all LOW), but a drive is refused, not silently dropped.
    assert [o["high"] for o in gpio.outputs()] == [False, False]
    with pytest.raises(GpioUnavailable):
        gpio.set("light", True)


def test_close_releases_backend_and_is_idempotent():
    backend = FakeBackend([o["pin"] for o in GPIO_OUTPUTS])
    gpio = GpioOutputs(backend_factory=lambda pins: backend)
    gpio.close()
    assert backend.closed is True
    gpio.close()  # no raise on second close
    # After close there is no backend to drive.
    with pytest.raises(GpioUnavailable):
        gpio.set("light", True)


# --- /api/gpio endpoints ---


@pytest.fixture
def gpio_client(tmp_path, monkeypatch):
    """A test client wired to a GpioOutputs on a fake backend. Returns (client, backend)."""
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))
    backend = FakeBackend([o["pin"] for o in GPIO_OUTPUTS])
    gpio = GpioOutputs(backend_factory=lambda pins: backend)
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False, gpio=gpio)
    return app.test_client(), backend


def test_get_gpio_lists_outputs(gpio_client):
    client, _ = gpio_client
    resp = client.get("/api/gpio")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is True
    assert [o["name"] for o in data["outputs"]] == ["light", "aux"]
    assert all(o["high"] is False for o in data["outputs"])


def test_post_gpio_sets_high_and_returns_state(gpio_client):
    client, backend = gpio_client
    resp = client.post("/api/gpio/light", json={"high": True})
    assert resp.status_code == 200
    data = resp.get_json()
    light = {o["name"]: o for o in data["outputs"]}["light"]
    assert light["high"] is True
    assert backend.writes[-1] == (27, True)


def test_post_gpio_unknown_name_404(gpio_client):
    client, _ = gpio_client
    resp = client.post("/api/gpio/nope", json={"high": True})
    assert resp.status_code == 404


@pytest.mark.parametrize("body", [{}, {"high": "yes"}, {"high": 1}, "notjson"])
def test_post_gpio_bad_body_400(gpio_client, body):
    client, _ = gpio_client
    if isinstance(body, str):
        resp = client.post("/api/gpio/light", data=body, content_type="application/json")
    else:
        resp = client.post("/api/gpio/light", json=body)
    assert resp.status_code == 400


def test_post_gpio_unavailable_backend_503(tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))
    gpio = GpioOutputs(backend_factory=_raising_factory)
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False, gpio=gpio)
    client = app.test_client()
    assert client.get("/api/gpio").get_json()["available"] is False
    resp = client.post("/api/gpio/light", json={"high": True})
    assert resp.status_code == 503

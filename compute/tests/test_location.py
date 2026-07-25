"""Tests for the compute-side location setting (admin-next P1 backend).

Two layers, mirroring ``test_edge_config.py`` / ``test_tuning.py``:

- ``Store.get_location`` / ``Store.set_location`` directly — the settings-KV
  round-trip and its "unset" contract.
- ``GET``/``POST /api/location`` through ``create_app`` + ``TestClient`` — the
  one-time edge seed, the manual override, and the 422 range validation. No
  real edge and no GPU: a fake client stands in for the Pi, exactly like
  ``ConfigClient``/``NoConfigClient`` in ``test_tuning.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


# --- Store.get_location / set_location ---------------------------------------


def test_get_location_unset_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get_location() is None


def test_set_then_get_location_round_trips(tmp_path):
    store = _store(tmp_path)
    store.set_location(55.676, 12.568)
    assert store.get_location() == (55.676, 12.568)


def test_set_location_default_source_is_manual(tmp_path):
    store = _store(tmp_path)
    store.set_location(1.0, 2.0)
    assert store.get_setting("location_source") == "manual"


def test_set_location_accepts_explicit_source(tmp_path):
    store = _store(tmp_path)
    store.set_location(1.0, 2.0, source="edge")
    assert store.get_setting("location_source") == "edge"


def test_set_location_overwrites_prior_value_and_source(tmp_path):
    store = _store(tmp_path)
    store.set_location(1.0, 2.0, source="edge")
    store.set_location(3.5, -4.5, source="manual")
    assert store.get_location() == (3.5, -4.5)
    assert store.get_setting("location_source") == "manual"


def test_get_location_ignores_corrupt_stored_value(tmp_path):
    # A hand-edited / corrupt settings row must degrade to "unset", not raise.
    store = _store(tmp_path)
    store.set_setting("location_lat", "not-a-number")
    store.set_setting("location_lon", "12.5")
    assert store.get_location() is None


def test_get_location_ignores_nonfinite_stored_value(tmp_path):
    # float() parses "nan"/"inf" WITHOUT raising, so a non-finite stored value must
    # be caught explicitly and degrade to "unset" — a non-finite coordinate would
    # otherwise poison the day/night classifier downstream.
    store = _store(tmp_path)
    store.set_setting("location_lon", "12.5")
    for bad in ("nan", "inf", "-inf"):
        store.set_setting("location_lat", bad)
        assert store.get_location() is None, bad


# --- Fake edge clients (mirrors test_tuning.py's ConfigClient/NoConfigClient) --


class NightLightConfigClient:
    """A stand-in edge whose ``get_config()`` embeds a ``night_light`` block."""

    def __init__(self, night_light: dict) -> None:
        self._config = {
            "device": 0,
            "rotation": 0,
            "clip": None,
            "fps": 5,
            "focus": None,
            "var_threshold": 16.0,
            "learning_rate": 0.001,
            "min_area": 0.01,
            "max_area_fraction": 0.6,
            "persistence": 2,
            "motion_downscale": 320,
            "night_light": night_light,
        }

    def get_config(self) -> dict:
        return dict(self._config)

    def iter_stream_reconnecting(self):
        return iter(())


class NoConfigClient:
    """A stand-in edge with no ``get_config`` — the unreachable/incompatible case."""

    def iter_stream_reconnecting(self):
        return iter(())


class BoomConfigClient:
    """A stand-in edge whose ``get_config()`` raises, like a connection failure."""

    def get_config(self) -> dict:
        raise ConnectionError("refused")

    def iter_stream_reconnecting(self):
        return iter(())


def _make_app(tmp_path, client=None) -> "tuple[TestClient, Store]":
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, client=client, start_collector=False)
    return TestClient(app), store


# --- GET /api/location: one-time edge seed -----------------------------------


def test_get_location_seeds_from_edge_night_light(tmp_path):
    client, store = _make_app(
        tmp_path,
        client=NightLightConfigClient({"latitude": 55.676, "longitude": 12.568}),
    )
    body = client.get("/api/location").json()
    assert body == {"latitude": 55.676, "longitude": 12.568, "source": "edge"}
    # Persisted, not just returned in-flight.
    assert store.get_location() == (55.676, 12.568)
    assert store.get_setting("location_source") == "edge"


def test_get_location_seed_is_idempotent_second_call_stays_seeded(tmp_path):
    # Second GET must not need the edge again — a client that would blow up if
    # called twice would still pass, since the value is already persisted. Model
    # that by swapping the app's client is impossible (client is closed over at
    # create_app time), so instead assert the store alone answers on a fresh app
    # with client=None after the seed.
    client, store = _make_app(
        tmp_path,
        client=NightLightConfigClient({"latitude": 10.0, "longitude": 20.0}),
    )
    client.get("/api/location")

    from compute.api.app import create_app

    app2 = create_app(store=store, client=None, start_collector=False)
    body = TestClient(app2).get("/api/location").json()
    assert body == {"latitude": 10.0, "longitude": 20.0, "source": "edge"}


def test_get_location_stays_unset_when_no_client(tmp_path):
    client, store = _make_app(tmp_path, client=None)
    body = client.get("/api/location").json()
    assert body == {"latitude": None, "longitude": None, "source": None}
    assert store.get_location() is None


def test_get_location_stays_unset_when_client_cannot_answer(tmp_path):
    client, store = _make_app(tmp_path, client=NoConfigClient())
    body = client.get("/api/location").json()
    assert body == {"latitude": None, "longitude": None, "source": None}
    assert store.get_location() is None


def test_get_location_stays_unset_when_edge_unreachable(tmp_path):
    client, store = _make_app(tmp_path, client=BoomConfigClient())
    body = client.get("/api/location").json()
    assert body == {"latitude": None, "longitude": None, "source": None}
    assert store.get_location() is None


def test_get_location_stays_unset_when_edge_night_light_missing(tmp_path):
    # A client whose config has no night_light key at all (an older Pi).
    bad = NightLightConfigClient({"latitude": 1.0, "longitude": 1.0})
    del bad._config["night_light"]
    client, store = _make_app(tmp_path, client=bad)
    body = client.get("/api/location").json()
    assert body == {"latitude": None, "longitude": None, "source": None}


@pytest.mark.parametrize(
    "night_light",
    [
        {"latitude": "not-a-number", "longitude": 12.568},
        {"latitude": 55.676, "longitude": "not-a-number"},
        {"latitude": 999.0, "longitude": 12.568},  # out of range
        {"latitude": 55.676, "longitude": -999.0},  # out of range
        {"latitude": True, "longitude": 12.568},  # bool must not pass as numeric
        {},  # missing both keys
    ],
)
def test_get_location_rejects_bad_edge_values_and_stays_unset(tmp_path, night_light):
    client, store = _make_app(tmp_path, client=NightLightConfigClient(night_light))
    body = client.get("/api/location").json()
    assert body == {"latitude": None, "longitude": None, "source": None}
    assert store.get_location() is None


# --- POST /api/location: manual set + validation ------------------------------


def test_post_location_sets_manual_value(tmp_path):
    client, store = _make_app(tmp_path, client=None)
    resp = client.post("/api/location", json={"latitude": 51.5, "longitude": -0.12})
    assert resp.status_code == 200
    assert resp.json() == {"latitude": 51.5, "longitude": -0.12, "source": "manual"}
    assert store.get_location() == (51.5, -0.12)
    assert store.get_setting("location_source") == "manual"


def test_post_location_overrides_an_edge_seeded_value(tmp_path):
    client, store = _make_app(
        tmp_path,
        client=NightLightConfigClient({"latitude": 55.676, "longitude": 12.568}),
    )
    client.get("/api/location")  # seeds source="edge"
    resp = client.post("/api/location", json={"latitude": 40.0, "longitude": -74.0})
    assert resp.status_code == 200
    assert resp.json()["source"] == "manual"
    assert store.get_location() == (40.0, -74.0)
    # A subsequent GET must reflect the manual value, not re-seed from the edge.
    body = client.get("/api/location").json()
    assert body == {"latitude": 40.0, "longitude": -74.0, "source": "manual"}


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 91.0, "longitude": 0.0},
        {"latitude": -91.0, "longitude": 0.0},
        {"latitude": 0.0, "longitude": 181.0},
        {"latitude": 0.0, "longitude": -181.0},
        {"latitude": "north", "longitude": 0.0},
        {"latitude": 0.0},  # missing longitude
    ],
)
def test_post_location_bad_input_is_422(tmp_path, payload):
    client, store = _make_app(tmp_path, client=None)
    resp = client.post("/api/location", json=payload)
    assert resp.status_code == 422
    assert store.get_location() is None


def test_post_location_boundary_values_are_accepted(tmp_path):
    # -90/90 and -180/180 are inclusive bounds, not off-by-one exclusions.
    client, store = _make_app(tmp_path, client=None)
    resp = client.post("/api/location", json={"latitude": 90.0, "longitude": -180.0})
    assert resp.status_code == 200
    assert store.get_location() == (90.0, -180.0)


def test_post_location_rejects_nan_and_infinity_as_422(tmp_path):
    # NaN/Infinity are accepted by the JSON parser and slip past Field bounds (every
    # NaN comparison is False); without allow_inf_nan=False they reach the response
    # encoder (allow_nan=False) and crash as a 500. They must surface as a clean 422.
    client, _ = _make_app(tmp_path)
    for raw in (
        '{"latitude": NaN, "longitude": 12.5}',
        '{"latitude": Infinity, "longitude": 12.5}',
        '{"latitude": 55.6, "longitude": -Infinity}',
    ):
        resp = client.post(
            "/api/location", content=raw, headers={"content-type": "application/json"}
        )
        assert resp.status_code == 422, raw


def test_post_location_rejects_boolean_as_422(tmp_path):
    # pydantic treats bool as an int subtype, so without the guard true/false would
    # coerce to 1.0/0.0 and persist silently (the edge-seed path already guards this).
    client, store = _make_app(tmp_path)
    resp = client.post("/api/location", json={"latitude": True, "longitude": 12.5})
    assert resp.status_code == 422
    assert store.get_location() is None

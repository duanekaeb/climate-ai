"""Setup: source and location, ecobee sign-in with MFA (adapter monkeypatched), thermostat and
sensor mapping, and the HomeKit pairing handshake. Secrets never appear in responses."""

from __future__ import annotations

import logging

import pytest

from climate.sources import ecobee as ecobee_src
from climate.store import secrets
from climate.store.db import session_scope
from climate.store.orm import EcobeeThermostat, HomekitDevice, UtilityEvent
from climate.timeutil import utcnow

PAIR_CODE = "123-45-678"


def _messages(r) -> str:
    d = r.json()["detail"]
    return d if isinstance(d, str) else " | ".join(x["msg"] for x in d)


def test_setup_state(owner):
    r = owner.get("/api/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["kind"] == "simulator"
    assert body["ecobee"]["signed_in"] is False and body["ecobee"]["mfa_pending"] is False
    assert body["ecobee"]["web_client_id"]
    assert [u["key"] for u in body["units"]] == ["main", "up", "bed"]
    assert len(body["rooms"]) == 11 and len(body["sensors"]) == 9
    assert body["secrets_ok"] is True
    assert body["homekit"] == {"enabled": False, "service_online": False, "devices": []}


def test_source_needs_ecobee_sign_in(owner):
    r = owner.post("/api/setup/source", json={"kind": "ecobee"})
    assert r.status_code == 409
    with session_scope() as s:
        secrets.put_secret(s, "ecobee_refresh_token", "rt-secret-value")
    r = owner.post("/api/setup/source", json={"kind": "ecobee", "homekit_enabled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["kind"] == "ecobee" and body["source"]["homekit_enabled"] is True
    assert body["ecobee"]["signed_in"] is True
    assert "rt-secret-value" not in r.text


def test_location(owner):
    bad = owner.put("/api/setup/location", json={"tz": "Nowhere/Land", "lat": 100, "lon": 5})
    assert bad.status_code == 422
    assert "Unknown time zone" in _messages(bad) and "Latitude" in _messages(bad)
    ok = owner.put("/api/setup/location", json={"tz": "America/Denver", "lat": 39.7, "lon": -105.0, "zip": "80202",
                                                "confirmed": True})
    assert ok.status_code == 200
    assert ok.json()["location"]["tz"] == "America/Denver" and ok.json()["location"]["confirmed"] is True


# --- ecobee sign-in ------------------------------------------------------------------------


def test_ecobee_login_with_mfa(owner, monkeypatch):
    calls = []
    pending = {"on": False}

    async def fake_login(email, password):
        calls.append(("login", email))
        pending["on"] = True
        return {"status": "mfa_required", "mfa_type": "otp"}

    async def fake_mfa(code):
        calls.append(("mfa", code))
        pending["on"] = False
        with session_scope() as s:
            secrets.put_secret(s, "ecobee_refresh_token", "rt-123")
        return {"status": "signed_in"}

    monkeypatch.setattr(ecobee_src, "start_login", fake_login)
    monkeypatch.setattr(ecobee_src, "submit_mfa", fake_mfa)
    monkeypatch.setattr(ecobee_src, "mfa_pending", lambda: (pending["on"], "otp" if pending["on"] else None))

    assert owner.post("/api/setup/ecobee/mfa", json={"code": "123456"}).status_code == 409  # nothing pending
    r = owner.post("/api/setup/ecobee/login", json={"email": " owner@example.com ", "password": "hunter2!"})
    assert r.json() == {"status": "mfa_required", "mfa_type": "otp", "error": None}
    st = owner.get("/api/setup").json()["ecobee"]
    assert st["mfa_pending"] is True and st["mfa_type"] == "otp" and st["signed_in"] is False
    assert owner.post("/api/setup/ecobee/mfa", json={"code": "12ab"}).status_code == 422
    r = owner.post("/api/setup/ecobee/mfa", json={"code": "123456"})
    assert r.json()["status"] == "signed_in"
    assert calls == [("login", "owner@example.com"), ("mfa", "123456")]
    st = owner.get("/api/setup")
    assert st.json()["ecobee"]["signed_in"] is True and st.json()["ecobee"]["mfa_pending"] is False
    assert "rt-123" not in st.text


def test_ecobee_login_errors_never_echo_the_password(owner, monkeypatch, caplog):
    async def auth_error(email, password):
        raise ecobee_src.EcobeeAuthError("Wrong email or password.")

    monkeypatch.setattr(ecobee_src, "start_login", auth_error)
    r = owner.post("/api/setup/ecobee/login", json={"email": "a@b.c", "password": "pw-SECRET-1"})
    assert r.status_code == 200 and r.json() == {"status": "error", "mfa_type": None, "error": "Wrong email or password."}

    async def crash(email, password):
        raise RuntimeError(f"boom {password}")

    monkeypatch.setattr(ecobee_src, "start_login", crash)
    with caplog.at_level(logging.DEBUG):
        r = owner.post("/api/setup/ecobee/login", json={"email": "a@b.c", "password": "pw-SECRET-2"})
    assert r.json()["status"] == "error"
    assert "pw-SECRET" not in r.text and "pw-SECRET" not in caplog.text


def test_ecobee_signout_forgets_the_token_even_if_the_adapter_fails(owner, monkeypatch):
    with session_scope() as s:
        secrets.put_secret(s, "ecobee_refresh_token", "rt-old")

    async def broken():
        raise RuntimeError("adapter down")

    monkeypatch.setattr(ecobee_src, "sign_out", broken)
    r = owner.post("/api/setup/ecobee/signout")
    assert r.status_code == 200
    assert r.json()["ecobee"]["signed_in"] is False


def test_thermostat_utility_enrollment(owner):
    now = utcnow()
    with session_scope() as s:
        s.add(EcobeeThermostat(identifier="311000000001", name="Hallway", model_number="nikeSmart", last_seen_at=now,
                               unit_key="main", settings={"drAccept": "askMe", "utility": {
                                   "name": "Example Power", "phone": "555-0100", "email": None, "web": "example.com"}}))
        s.add(EcobeeThermostat(identifier="311000000002", name="Toy Room", model_number="attisRetail", last_seen_at=now,
                               unit_key="up", settings={"drAccept": "always", "utility": None}))
        s.add(EcobeeThermostat(identifier="311000000003", name="Bedroom", model_number="nikeSmart", last_seen_at=now,
                               unit_key="bed", settings={"utility": {"name": ""}}))
        s.flush()
        s.add(UtilityEvent(unit_key="up", event_key="link:x", status="ended", first_seen_at=now, last_seen_at=now))
    tstats = {t["identifier"]: t for t in owner.get("/api/setup").json()["ecobee"]["thermostats"]}
    hallway, toy, bedroom = tstats["311000000001"], tstats["311000000002"], tstats["311000000003"]
    assert hallway["utility"] == {"name": "Example Power", "phone": "555-0100", "email": None, "web": "example.com"}
    assert (hallway["dr_accept"], hallway["enrolled"]) == ("askMe", True)
    assert (toy["utility"], toy["dr_accept"], toy["enrolled"]) == (None, "always", True)  # an event was seen there
    assert (bedroom["utility"], bedroom["dr_accept"], bedroom["enrolled"]) == (None, None, None)  # unknown, not "no"


def test_ecobee_map(owner):
    with session_scope() as s:
        for ident, name, model in (("311000000001", "Hallway", "nikeSmart"), ("311000000002", "Toy Room", "attisRetail")):
            s.add(EcobeeThermostat(identifier=ident, name=name, model_number=model, last_seen_at=utcnow(),
                                   sensors=[{"id": "ei:0", "name": name}]))
    r = owner.post("/api/setup/ecobee/map", json={"identifier": "311000000001", "unit_key": "main"})
    assert r.status_code == 200
    units = {u["key"]: u for u in r.json()["units"]}
    assert units["main"]["ecobee_identifier"] == "311000000001" and units["main"]["thermostat_model"] == "nikeSmart"
    tstats = {t["identifier"]: t for t in r.json()["ecobee"]["thermostats"]}
    assert tstats["311000000001"]["unit_key"] == "main"

    clash = owner.post("/api/setup/ecobee/map", json={"identifier": "311000000002", "unit_key": "main"})
    assert clash.status_code == 409
    # move the first thermostat to another unit: its old unit is released
    r = owner.post("/api/setup/ecobee/map", json={"identifier": "311000000001", "unit_key": "up"})
    units = {u["key"]: u for u in r.json()["units"]}
    assert units["main"]["ecobee_identifier"] is None and units["up"]["ecobee_identifier"] == "311000000001"
    r = owner.post("/api/setup/ecobee/map", json={"identifier": "311000000001", "unit_key": None})
    assert all(u["ecobee_identifier"] is None for u in r.json()["units"])
    assert owner.post("/api/setup/ecobee/map", json={"identifier": "nope", "unit_key": "up"}).status_code == 404
    assert owner.post("/api/setup/ecobee/map", json={"identifier": "311000000002", "unit_key": "attic"}).status_code == 404


def test_sensor_map(owner):
    r = owner.post("/api/setup/sensors/map", json={"sensor_key": "main.school_room", "ecobee_sensor_id": "rs:100"})
    assert r.status_code == 200
    sensors = {s["key"]: s for s in r.json()["sensors"]}
    assert sensors["main.school_room"]["ecobee_sensor_id"] == "rs:100"
    clash = owner.post("/api/setup/sensors/map", json={"sensor_key": "main.kitchen", "ecobee_sensor_id": "rs:100"})
    assert clash.status_code == 409 and "main.school_room" in _messages(clash)
    # homekit_aid alone leaves the ecobee id untouched
    r = owner.post("/api/setup/sensors/map", json={"sensor_key": "main.school_room", "homekit_aid": 5_000_000_000})
    s = {x["key"]: x for x in r.json()["sensors"]}["main.school_room"]
    assert (s["ecobee_sensor_id"], s["homekit_aid"]) == ("rs:100", 5_000_000_000)
    r = owner.post("/api/setup/sensors/map", json={"sensor_key": "main.school_room", "ecobee_sensor_id": None})
    assert {x["key"]: x for x in r.json()["sensors"]}["main.school_room"]["ecobee_sensor_id"] is None
    assert owner.post("/api/setup/sensors/map", json={"sensor_key": "main.school_room"}).status_code == 422
    assert owner.post("/api/setup/sensors/map", json={"sensor_key": "nope", "homekit_aid": 1}).status_code == 404


# --- HomeKit pairing ----------------------------------------------------------------------


def _device(device_id="aa:bb:cc:dd:ee:01", name="Hallway ecobee", online=True, flags=1, state="none", **kw):
    with session_scope() as s:
        s.add(HomekitDevice(device_id=device_id, name=name, online=online, status_flags=flags, pairing_state=state,
                            address="192.168.1.20", port=1200, **kw))


def _set_state(device_id: str, state: str) -> None:
    with session_scope() as s:
        s.get(HomekitDevice, device_id).pairing_state = state


def _db_device(device_id: str) -> HomekitDevice:
    with session_scope() as s:
        return s.get(HomekitDevice, device_id)


def test_homekit_pairing_handshake(owner):
    dev = "aa:bb:cc:dd:ee:01"
    _device()
    r = owner.post("/api/setup/homekit/pair", json={"device_id": "AA:BB:CC:DD:EE:01", "alias": "hallway", "unit_key": "main"})
    assert r.status_code == 200, r.text
    d = r.json()["homekit"]["devices"][0]
    assert (d["pairing_state"], d["alias"], d["unit_key"], d["unpaired"]) == ("requested", "hallway", "main", True)
    assert owner.post("/api/setup/homekit/pair", json={"device_id": dev, "alias": "hallway", "unit_key": "main"}).status_code == 409
    assert owner.post("/api/setup/homekit/code", json={"device_id": dev, "code": PAIR_CODE}).status_code == 409

    _set_state(dev, "awaiting_code")  # the homekit service started pair-setup
    assert owner.post("/api/setup/homekit/code", json={"device_id": dev, "code": "12345678"}).status_code == 422
    r = owner.post("/api/setup/homekit/code", json={"device_id": dev, "code": PAIR_CODE})
    assert r.status_code == 200
    assert r.json()["homekit"]["devices"][0]["pairing_state"] == "code_submitted"
    assert PAIR_CODE not in r.text and "pairing_code" not in r.text
    assert _db_device(dev).pairing_code == PAIR_CODE  # handed to the service through the DB
    assert PAIR_CODE not in owner.get("/api/setup").text

    _set_state(dev, "paired")
    r = owner.post("/api/setup/homekit/unpair", json={"device_id": dev})
    assert r.status_code == 200 and r.json()["homekit"]["devices"][0]["pairing_state"] == "unpair_requested"
    assert _db_device(dev).pairing_code is None
    assert owner.post("/api/setup/homekit/unpair", json={"device_id": dev}).status_code == 409


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"online": False}, "not on the network"),
        ({"flags": 0}, "paired with another controller"),
        ({"state": "paired"}, "already paired"),
    ],
)
def test_homekit_pair_refused(owner, kw, needle):
    _device(**kw)
    r = owner.post("/api/setup/homekit/pair", json={"device_id": "aa:bb:cc:dd:ee:01", "alias": "hallway", "unit_key": "main"})
    assert r.status_code == 409 and needle in _messages(r)


def test_homekit_pair_alias_and_unit_conflicts(owner):
    _device("aa:bb:cc:dd:ee:01", alias="hallway", state="paired", unit_key="main")
    _device("aa:bb:cc:dd:ee:02", name="Bedroom ecobee")
    clash = owner.post("/api/setup/homekit/pair", json={"device_id": "aa:bb:cc:dd:ee:02", "alias": "hallway", "unit_key": "bed"})
    assert clash.status_code == 409 and "alias" in _messages(clash)
    clash = owner.post("/api/setup/homekit/pair", json={"device_id": "aa:bb:cc:dd:ee:02", "alias": "bedroom", "unit_key": "main"})
    assert clash.status_code == 409 and "already has a HomeKit device" in _messages(clash)
    ok = owner.post("/api/setup/homekit/pair", json={"device_id": "aa:bb:cc:dd:ee:02", "alias": "bedroom", "unit_key": "bed"})
    assert ok.status_code == 200
    assert owner.post("/api/setup/homekit/pair", json={"device_id": "zz", "alias": "bedroom", "unit_key": "bed"}).status_code == 404
    assert owner.post("/api/setup/homekit/pair", json={"device_id": "aa:bb:cc:dd:ee:02", "alias": "Bad Alias",
                                                       "unit_key": "bed"}).status_code == 422


def test_setup_has_no_secret_columns(owner):
    _device(pairing_code="999-99-999", state="code_submitted")
    with session_scope() as s:
        secrets.put_secret(s, "homekit_pairing:hallway", '{"iOSDeviceLTSK": "private-key-material"}')
    text = owner.get("/api/setup").text
    assert "999-99-999" not in text and "private-key-material" not in text and "iOSDeviceLTSK" not in text

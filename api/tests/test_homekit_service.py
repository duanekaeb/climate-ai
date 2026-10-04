"""The homekit service against the test database, with an aiohomekit-shaped fake controller.

ingest_live_readings and the alert helpers belong to other modules; they are replaced here
with recorders so these tests do not depend on them being finished.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("aiohomekit")

from aiohomekit.exceptions import AuthenticationError
from sqlalchemy import select, update

from climate import notify
from climate.collector import homekit_service as svc_mod
from climate.collector import ingest
from climate.collector.homekit_service import HomekitService, save_pairing, secret_key
from climate.sources import homekit as hk
from climate.store import secrets
from climate.store.app_settings import SourceSettings, get_heartbeat, put_setting
from climate.store.db import session_scope
from climate.store.orm import ControlAction, HomekitDevice, SecretRow, Sensor, Unit
from tests.test_homekit_fakes import SECRET_LTSK, FakeController, FakeDevice, disconnected

BIG_AID = 4295608971


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def recorder(monkeypatch):
    rec: dict[str, list] = {"readings": [], "alerts": [], "resolved": []}

    def fake_ingest(session, readings, source="homekit"):
        assert source == "homekit"
        rec["readings"].extend(readings)

    def fake_raise(session, kind, level, title, body="", dedupe_key=None):
        rec["alerts"].append((kind, level, dedupe_key))
        return 1

    def fake_resolve(session, dedupe_key):
        rec["resolved"].append(dedupe_key)

    monkeypatch.setattr(ingest, "ingest_live_readings", fake_ingest)
    monkeypatch.setattr(notify, "raise_alert", fake_raise)
    monkeypatch.setattr(notify, "resolve_alert", fake_resolve)
    return rec


@pytest.fixture
def env(db, tmp_path, recorder):
    put_setting(db, "source", SourceSettings(homekit_enabled=True))
    db.commit()
    dev = FakeDevice()
    ctl = FakeController([dev])
    clock = Clock()
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl)
    service = HomekitService(bridge, clock=clock)
    return {"db": db, "dev": dev, "ctl": ctl, "clock": clock, "bridge": bridge, "svc": service, "rec": recorder}


def device_row(db, device_id: str) -> HomekitDevice:
    db.expire_all()
    row = db.get(HomekitDevice, device_id)
    assert row is not None
    return row


def add_device(db, dev: FakeDevice, state: str = "none", alias: str | None = "hallway", unit: str | None = "main",
               code: str | None = None) -> None:
    db.add(HomekitDevice(device_id=dev.id, name=dev.name, alias=alias, unit_key=unit, pairing_state=state,
                         pairing_code=code))
    db.commit()


def submit_code(db, dev: FakeDevice, code: str) -> None:
    db.execute(update(HomekitDevice).where(HomekitDevice.device_id == dev.id)
               .values(pairing_state="code_submitted", pairing_code=code))
    db.commit()


async def paired_and_ready(env) -> None:
    """A device already paired (keys in the DB), loaded, inventoried and subscribed."""
    _db, dev = env["db"], env["dev"]
    dev.paired, dev.status_flags = True, 0
    with session_scope() as s:
        save_pairing(s, dev.id, "hallway", "main", dev.pairing_data(), None, env["clock"]())
    await env["bridge"].start()
    await env["svc"].reconcile()
    assert env["svc"]._states["hallway"].ready


# --- pairing handshake ----------------------------------------------------------------


async def test_handshake_pairs_and_saves_keys_encrypted_before_closing(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    add_device(db, dev, "requested")

    def check_saved_at_close() -> None:
        with session_scope() as s:
            data = secrets.get_secret_json(s, secret_key("hallway"))
        assert data is not None and data["iOSDeviceLTSK"] == SECRET_LTSK
        dev.log.append("keys_were_committed")

    dev.on_temp_close = check_saved_at_close
    await env["bridge"].start()
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert row.pairing_state == "awaiting_code" and row.pairing_error is None
    assert dev.log == ["start:hallway"]

    submit_code(db, dev, "123-45-678")
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert (row.pairing_state, row.pairing_code, row.pairing_error) == ("paired", None, None)
    assert dev.log == ["start:hallway", "finished", "close_temporary", "keys_were_committed"]
    assert db.get(Unit, "main").homekit_device_id == dev.id
    cipher = db.execute(select(SecretRow.ciphertext).where(SecretRow.key == secret_key("hallway"))).scalar_one()
    assert SECRET_LTSK.encode() not in bytes(cipher)  # encrypted at rest

    # the same pass of reconcile loads it, stores the inventory and maps sensors by name
    await svc.reconcile()
    row = device_row(db, dev.id)
    assert [a["aid"] for a in row.accessories] == [1, BIG_AID]
    aids = {s.key: s.homekit_aid for s in db.execute(select(Sensor).where(Sensor.unit_key == "main")).scalars()}
    assert aids["main.hallway_tstat"] == 1 and aids["main.kitchen"] == BIG_AID
    assert aids["main.school_room"] is None
    assert {s.homekit_aid for s in db.execute(select(Sensor).where(Sensor.unit_key != "main")).scalars()} == {None}


async def test_wrong_code_fails_and_clears_the_code(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    add_device(db, dev, "requested")
    await env["bridge"].start()
    await svc.process_pairing()
    submit_code(db, dev, "000-00-000")
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert (row.pairing_state, row.pairing_code, row.pairing_error) == ("failed", None, hk.WRONG_CODE_MSG)
    with session_scope() as s:
        assert secrets.get_secret_json(s, secret_key("hallway")) is None


async def test_already_paired_thermostat_explains_disconnect(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    dev.paired, dev.status_flags = True, 0
    add_device(db, dev, "requested")
    await env["bridge"].start()
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert row.pairing_state == "failed" and "Disconnect from HomeKit" in (row.pairing_error or "")


async def test_code_without_a_session_and_timeouts_fail(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    add_device(db, dev, "code_submitted", code="123-45-678")
    await env["bridge"].start()
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert (row.pairing_state, row.pairing_error, row.pairing_code) == ("failed", hk.NO_PENDING_MSG, None)

    db.execute(update(HomekitDevice).values(pairing_state="requested"))
    db.commit()
    svc.code_timeout_s = 0.0
    await svc.process_pairing()
    assert device_row(db, dev.id).pairing_state == "awaiting_code"
    await asyncio.sleep(0.01)
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert row.pairing_state == "failed" and "Timed out" in (row.pairing_error or "")
    assert not env["bridge"].has_pending(dev.id)


async def test_bad_code_format_goes_back_to_awaiting_code(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    add_device(db, dev, "requested")
    await env["bridge"].start()
    await svc.process_pairing()
    submit_code(db, dev, "12345678")
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert (row.pairing_state, row.pairing_error) == ("awaiting_code", hk.BAD_CODE_FORMAT_MSG)
    submit_code(db, dev, "123-45-678")
    await svc.process_pairing()
    assert device_row(db, dev.id).pairing_state == "paired"


async def test_unpair_removes_on_device_and_deletes_keys(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    await paired_and_ready(env)
    db.execute(update(HomekitDevice).values(pairing_state="unpair_requested"))
    db.commit()
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert (row.pairing_state, row.alias, row.unit_key, row.accessories) == ("none", None, None, None)
    assert "removed_on_device" in dev.log and not dev.paired
    assert db.get(Unit, "main").homekit_device_id is None
    with session_scope() as s:
        assert secrets.get_secret_json(s, secret_key("hallway")) is None
    assert not env["bridge"].is_loaded("hallway")


async def test_unpair_unreachable_keeps_keys_and_stays_paired(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    await paired_and_ready(env)
    env["ctl"].aliases["hallway"].fail_always = disconnected()
    db.execute(update(HomekitDevice).values(pairing_state="unpair_requested"))
    db.commit()
    await svc.process_pairing()
    row = device_row(db, dev.id)
    assert row.pairing_state == "paired" and "must be reachable" in (row.pairing_error or "")
    with session_scope() as s:
        assert secrets.get_secret_json(s, secret_key("hallway")) is not None
    await svc.reconcile()  # loads it again from the saved keys
    assert env["bridge"].is_loaded("hallway")


async def test_paired_row_without_keys_fails(env):
    db, dev, svc = env["db"], env["dev"], env["svc"]
    add_device(db, dev, "paired")
    await env["bridge"].start()
    await svc.reconcile()
    row = device_row(db, dev.id)
    assert row.pairing_state == "failed" and row.pairing_error == svc_mod.KEYS_MISSING_MSG


# --- discovery, polling, events -------------------------------------------------------


async def test_discovery_upserts_devices(env):
    db, svc = env["db"], env["svc"]
    other = FakeDevice("aa:bb:cc:dd:ee:02", "Bedroom", status_flags=0)
    env["ctl"].devices[other.id] = other
    await env["bridge"].start()
    await svc.sync_discovery()
    row = device_row(db, other.id)
    assert (row.name, row.model, row.status_flags, row.port, row.online) == ("Bedroom", "ECB501", 0, 51826, True)
    assert row.last_seen_at is not None and row.pairing_state == "none"
    other.visible = False
    await svc.sync_discovery()
    assert device_row(db, other.id).online is False


async def test_poll_pushes_readings_through_ingest(env):
    rec = env["rec"]
    await paired_and_ready(env)
    await env["svc"].poll_due()
    by_key = {r.sensor_key: r for r in rec["readings"]}
    assert set(by_key) == {"main.hallway_tstat", "main.kitchen"}
    hall, kitchen = by_key["main.hallway_tstat"], by_key["main.kitchen"]
    assert (hall.temp_f, hall.humidity, hall.occupied) == (71.6, 45.0, True)
    assert (kitchen.temp_f, kitchen.occupied, kitchen.seconds_since_occupancy, kitchen.motion) == (70.7, False, -1, False)
    row = device_row(env["db"], env["dev"].id)
    assert row.online is True and row.last_seen_at is not None
    rec["readings"].clear()
    await env["svc"].poll_due()  # not due again yet
    assert rec["readings"] == []


async def test_three_failed_polls_mark_the_device_offline(env):
    db, clock, svc = env["db"], env["clock"], env["svc"]
    await paired_and_ready(env)
    env["ctl"].aliases["hallway"].fail_always = disconnected()
    for expected in (True, True, False):
        await svc.poll_due()
        db.expire_all()
        assert device_row(db, env["dev"].id).online is expected
        clock.advance(61)
    env["ctl"].aliases["hallway"].fail_always = None
    await svc.poll_due()
    assert device_row(db, env["dev"].id).online is True


async def test_pairing_removed_on_device_asks_for_re_pair(env):
    db, svc, rec = env["db"], env["svc"], env["rec"]
    await paired_and_ready(env)
    env["ctl"].aliases["hallway"].fail_always = AuthenticationError("unknown controller")
    await svc.poll_due()
    row = device_row(db, env["dev"].id)
    assert row.pairing_error == hk.REPAIR_MSG and row.online is False and row.pairing_state == "paired"
    assert rec["alerts"] == [("homekit_pairing", "error", "homekit_repair:hallway")]
    env["ctl"].aliases["hallway"].fail_always = None
    env["clock"].advance(61)
    await svc.poll_due()
    row = device_row(db, env["dev"].id)
    assert row.pairing_error is None and row.online is True
    assert rec["resolved"] == ["homekit_repair:hallway"]


async def test_pushed_events_are_queued_then_drained(env):
    rec, dev = env["rec"], env["dev"]
    await paired_and_ready(env)
    pairing = env["ctl"].aliases["hallway"]
    pairing.push({dev.k(BIG_AID, "OCCUPANCY_DETECTED"): {"value": 1}})
    assert rec["readings"] == []  # nothing written from inside the callback
    await env["svc"].drain_events()
    (reading,) = rec["readings"]
    assert reading.sensor_key == "main.kitchen" and reading.occupied is True


# --- queued HomeKit actions -----------------------------------------------------------


def queue(db, request: dict, ts: datetime | None = None, unit: str = "main") -> int:
    row = ControlAction(unit_key=unit, actor="owner", mode="act", channel="homekit", action="set_hold",
                        status="queued", reason="test", request=request)
    if ts is not None:
        row.ts = ts
    db.add(row)
    db.commit()
    return row.id


def action(db, action_id: int) -> ControlAction:
    db.expire_all()
    row = db.get(ControlAction, action_id)
    assert row is not None
    return row


async def test_queued_climate_hold_is_written_and_verified(env):
    db, _dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    until = clock() + timedelta(minutes=90)
    aid = queue(db, {"kind": "climate_hold", "climate": "away", "until": until.isoformat()})
    await env["svc"].execute_actions()
    row = action(db, aid)
    assert row.status == "verified" and row.readback_ok is True and row.error is None
    assert row.readback["current_mode"] == 2 and row.readback["timestamp"].endswith("R")
    assert row.before["current_mode"] == 0 and row.completed_at is not None
    puts = [p for kind, p in env["ctl"].aliases["hallway"].calls if kind == "put"]
    assert [len(p) for p in puts] == [1, 1]


async def test_rejected_write_is_recorded_as_failed(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    env["ctl"].aliases["hallway"].put_status = {dev.k(1, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE"): -70404}
    aid = queue(db, {"kind": "climate_hold", "climate": "sleep", "until": (clock() + timedelta(hours=1)).isoformat()})
    await env["svc"].execute_actions()
    row = action(db, aid)
    assert row.status == "failed" and "rejected" in (row.error or "") and row.readback_ok is None


async def test_invalid_or_stale_requests_never_write(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    too_long = queue(db, {"kind": "climate_hold", "climate": "away", "until": (clock() + timedelta(hours=5)).isoformat()})
    bad_climate = queue(db, {"kind": "climate_hold", "climate": "vacation", "until": clock().isoformat()})
    unknown = queue(db, {"kind": "set_temperature"})
    no_unit = queue(db, {"kind": "clear_hold"}, unit="bed")
    expired = queue(db, {"kind": "clear_hold"}, ts=clock() - timedelta(minutes=30))
    await env["svc"].execute_actions()
    for aid, needle in ((too_long, "must be 5 min"), (bad_climate, "climate must be"), (unknown, "unknown"),
                        (no_unit, "no paired HomeKit"), (expired, "expired")):
        row = action(db, aid)
        assert row.status == "failed" and needle in (row.error or ""), (aid, row.error)
    assert dev.writes == []


async def test_queued_clear_hold(env):
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    aid = queue(db, {"kind": "clear_hold"})
    await env["svc"].execute_actions()
    row = action(db, aid)
    assert row.status == "verified" and row.readback["current_mode"] == 0
    assert dev.writes == [(1, dev.iids[(1, "VENDOR_ECOBEE_CLEAR_HOLD")], True)]


# --- the loop -------------------------------------------------------------------------


async def test_disabled_service_does_not_start_the_controller(env):
    db, ctl = env["db"], env["ctl"]
    put_setting(db, "source", SourceSettings(homekit_enabled=False))
    db.commit()
    svc = env["svc"]
    svc.disabled_recheck_s = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert not ctl.started
    db.expire_all()
    hb = get_heartbeat(db, "homekit")
    assert hb is not None and hb.ok and hb.detail["enabled"] is False


async def test_run_executes_steps_and_beats(env):
    db, ctl, dev = env["db"], env["ctl"], env["dev"]
    add_device(db, dev, "requested")
    svc = env["svc"]
    svc.tick_s = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if device_row(db, dev.id).pairing_state == "awaiting_code":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert ctl.started and ctl.stopped
    assert device_row(db, dev.id).pairing_state == "awaiting_code"
    db.expire_all()
    hb = get_heartbeat(db, "homekit")
    assert hb is not None and hb.ok and hb.detail["enabled"] is True and hb.detail["paired"] == 0

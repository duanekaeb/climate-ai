"""HomekitBridge against aiohomekit-shaped fakes: no network, no database."""

from __future__ import annotations

import asyncio
import logging
import stat
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("aiohomekit")

from aiohomekit.exceptions import (
    AccessoryNotFoundError,
    AuthenticationError,
    BusyError,
    MaxPeersError,
    MaxTriesError,
)
from aiohomekit.model.characteristics import CharacteristicsTypes as CT
from aiohomekit.model.services import ServicesTypes as ST

from climate.sources import homekit as hk
from tests.test_homekit_fakes import (
    HOME_TARGET_HEAT,
    SECRET_LTSK,
    FakeController,
    FakeDevice,
    disconnected,
)

TZ = "America/Chicago"
BIG_AID = 4295608971  # SmartSensor aid seen on ecobee3 lite fw 4.8 (> 2^32)


def make_bridge(tmp_path, devices):
    ctl = FakeController(devices)
    seen: list[tuple[str, set[int]]] = []
    bridge = hk.HomekitBridge(
        tmp_path / "hk", controller_factory=lambda _p: ctl, on_event=lambda a, aids: seen.append((a, set(aids)))
    )
    return bridge, ctl, seen


async def loaded(tmp_path, dev: FakeDevice | None = None):
    dev = dev or FakeDevice(status_flags=0)
    bridge, ctl, seen = make_bridge(tmp_path, [dev])
    await bridge.start()
    await bridge.load("hallway", dev.pairing_data())
    await bridge.inventory("hallway")
    return bridge, ctl, dev, seen


def pairing(ctl: FakeController):
    return ctl.aliases["hallway"]


# --- constants ------------------------------------------------------------------------


def test_type_constants_match_aiohomekit():
    for name, uuid in hk.CHAR_TYPES.items():
        assert getattr(CT, name) == uuid, name
    assert hk.FORBIDDEN_WRITE_TYPES == {
        CT.VENDOR_ECOBEE_HOME_TARGET_HEAT, CT.VENDOR_ECOBEE_HOME_TARGET_COOL,
        CT.VENDOR_ECOBEE_SLEEP_TARGET_HEAT, CT.VENDOR_ECOBEE_SLEEP_TARGET_COOL,
        CT.VENDOR_ECOBEE_AWAY_TARGET_HEAT, CT.VENDOR_ECOBEE_AWAY_TARGET_COOL,
    }
    assert hk.INFO_TYPES == {"NAME": CT.NAME, "MODEL": CT.MODEL, "FIRMWARE_REVISION": CT.FIRMWARE_REVISION}
    assert hk.SERVICE_ACCESSORY_INFORMATION == ST.ACCESSORY_INFORMATION
    assert hk.SERVICE_THERMOSTAT == ST.THERMOSTAT
    assert not (hk.ALLOWED_WRITE_KEYS & hk.PUSH_KEYS)


async def test_start_creates_private_state_dir_and_no_pairing_file(tmp_path):
    bridge, ctl, _ = make_bridge(tmp_path, [])
    await bridge.start()
    state = tmp_path / "hk"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "charmap.json").stat().st_mode) == 0o600
    assert ctl.started
    await bridge.stop()
    assert ctl.stopped
    assert sorted(p.name for p in state.iterdir()) == ["charmap.json"]


# --- pure helpers ---------------------------------------------------------------------


def test_timestamp_suffix_and_format():
    assert hk.timestamp_suffix("2025-04-06T23:30:00-05:00R") == "R"
    assert hk.timestamp_suffix("2035-01-03T00:00:00+00:00T") == "T"
    assert hk.timestamp_suffix("2024-10-19T20:35:05-04:00Q") == "Q"
    assert hk.timestamp_suffix("2024-01-01T12:00:00-05:00") == ""
    with pytest.raises(hk.HomekitWriteError):
        hk.timestamp_suffix("tomorrow")
    until = datetime(2026, 10, 4, 20, 30, 12, 999, tzinfo=UTC)
    assert hk.format_hold_end(until, TZ, "T") == "2026-10-04T15:30:12-05:00T"
    assert hk.parse_timestamp("2026-10-04T15:30:12-05:00T", TZ) == until.replace(microsecond=0)
    assert hk.parse_timestamp("2026-10-04T15:30:12", TZ) == until.replace(microsecond=0)


def test_readings_convert_and_keep_unknowns_unknown():
    ts = datetime(2026, 10, 4, 12, tzinfo=UTC)
    r = hk.reading_from_values("main.kitchen", {
        "TEMPERATURE_CURRENT": 22.0, "OCCUPANCY_DETECTED": 1, "MOTION_DETECTED": False,
        "VENDOR_ECOBEE_OCCUPANCY_LAST_ACTIVATION": -1, "VENDOR_ECOBEE_MOTION_LAST_ACTIVATION": 600,
        "STATUS_ACTIVE": True, "STATUS_LO_BATT": 1,
    }, ts)
    assert r is not None
    assert (r.temp_f, r.occupied, r.motion, r.battery_low, r.online) == (71.6, True, False, True, True)
    assert (r.seconds_since_occupancy, r.seconds_since_motion) == (-1, 600)  # -1 = never
    # a stale 100 °C after a reboot is dropped, never stored
    stale = hk.reading_from_values("main.kitchen", {"TEMPERATURE_CURRENT": 100.0, "OCCUPANCY_DETECTED": 0}, ts)
    assert stale is not None and stale.temp_f is None and stale.occupied is False
    assert hk.reading_from_values("main.kitchen", {}, ts) is None
    offline = hk.reading_from_values("main.kitchen", {"STATUS_ACTIVE": False}, ts)
    assert offline is not None and offline.online is False and offline.temp_f is None


def test_build_aid_map_by_name_including_big_aids():
    inv = [
        {"aid": 1, "name": "Hallway"},
        {"aid": BIG_AID, "name": "Kitchen"},
        {"aid": 2, "name": "SCHOOL room"},
        {"aid": 3, "name": "Living-Room"},
        {"aid": 5, "name": "Garage"},
        {"aid": 6, "name": "Office"},  # a bed-wing sensor name: not this unit's
    ]
    m = hk.build_aid_map(inv, "main")
    assert m == {1: "main.hallway_tstat", BIG_AID: "main.kitchen", 2: "main.school_room", 3: "main.living_room"}
    assert BIG_AID > 2**32

    up = hk.build_aid_map([{"aid": 1, "name": "Toy Room"}, {"aid": 4295608999, "name": "Toy Room"},
                           {"aid": 7, "name": "Girls' Room"}], "up")
    assert up == {1: "up.toy_room_tstat", 4295608999: "up.toy_room", 7: "up.girls_room"}

    # an existing (owner) mapping that is present wins over a name match
    owned = hk.build_aid_map(inv, "main", existing={"main.kitchen": 5})
    assert owned[5] == "main.kitchen" and BIG_AID not in owned
    # an existing mapping to an aid this pairing does not have is ignored
    stale = hk.build_aid_map(inv, "main", existing={"main.kitchen": 999})
    assert stale[BIG_AID] == "main.kitchen"


# --- inventory / read -----------------------------------------------------------------


async def test_inventory_indexes_thermostat_and_sensors(tmp_path):
    bridge, _ctl, dev, _ = await loaded(tmp_path)
    inv = bridge.inventory_of("hallway")
    assert [a["aid"] for a in inv] == [1, BIG_AID]
    tstat, sensor = inv
    assert (tstat["name"], tstat["model"], tstat["firmware"]) == ("Hallway", "ECB501", "4.7.340214")
    assert tstat["chars"]["TEMPERATURE_CURRENT"]["iid"] == dev.iids[(1, "TEMPERATURE_CURRENT")]
    assert tstat["chars"]["VENDOR_ECOBEE_TIMESTAMP"]["perms"] == ["pr", "pw"]
    assert "VENDOR_ECOBEE_SET_HOLD_SCHEDULE" in tstat["chars"]
    assert all(c["type"] != HOME_TARGET_HEAT for c in tstat["chars"].values())
    assert (sensor["name"], sensor["model"]) == ("Kitchen", "EBRSE4")
    assert set(sensor["chars"]) >= {"TEMPERATURE_CURRENT", "OCCUPANCY_DETECTED", "MOTION_DETECTED",
                                    "VENDOR_ECOBEE_OCCUPANCY_LAST_ACTIVATION", "BATTERY_LEVEL", "STATUS_LO_BATT"}


async def test_read_polls_in_batches_of_49_one_request_at_a_time(tmp_path):
    sensors = [(4295608971 + i, f"Sensor {i}", "EBRSE4") for i in range(12)]
    dev = FakeDevice(status_flags=0, sensors=sensors)
    bridge, ctl, dev, _ = await loaded(tmp_path, dev)
    p = pairing(ctl)
    p.calls.clear()
    a, b = await asyncio.gather(bridge.read("hallway"), bridge.read("hallway"))
    assert a.ok and b.ok
    gets = [payload for kind, payload in p.calls if kind == "get"]
    readable = 8 * 12 + 8  # 8 readable per sensor and on the thermostat (write-only ones skipped)
    assert all(len(g) <= 49 for g in gets)
    assert len(gets) == 2 * -(-readable // 49)
    assert sum(len(g) for g in gets) == 2 * readable
    assert p.max_in_flight == 1
    assert a.values[1]["VENDOR_ECOBEE_CURRENT_MODE"] == 0
    assert "VENDOR_ECOBEE_SET_HOLD_SCHEDULE" not in a.values[1]


async def test_status_entries_are_missing_data(tmp_path):
    bridge, ctl, dev, _ = await loaded(tmp_path)
    pairing(ctl).missing = {dev.k(BIG_AID, "TEMPERATURE_CURRENT")}
    res = await bridge.read("hallway")
    assert res.ok and res.missing == 1
    assert "TEMPERATURE_CURRENT" not in res.values[BIG_AID]
    readings = hk.readings_from_values(res.values, {1: "main.hallway_tstat", BIG_AID: "main.kitchen"},
                                       datetime.now(UTC))
    kitchen = next(r for r in readings if r.sensor_key == "main.kitchen")
    assert kitchen.temp_f is None and kitchen.occupied is False
    hall = next(r for r in readings if r.sensor_key == "main.hallway_tstat")
    assert (hall.temp_f, hall.humidity, hall.occupied) == (71.6, 45.0, True)


async def test_three_failed_polls_make_it_unavailable(tmp_path):
    bridge, ctl, _dev, _ = await loaded(tmp_path)
    pairing(ctl).fail_always = disconnected()
    for expected in (True, True, False):
        res = await bridge.read("hallway")
        assert not res.ok
        assert res.available is expected
    assert not bridge.is_available("hallway")
    pairing(ctl).fail_always = None
    res = await bridge.read("hallway")
    assert res.ok and bridge.is_available("hallway")


async def test_accessory_not_found_is_unavailable_at_once(tmp_path):
    bridge, ctl, _dev, _ = await loaded(tmp_path)
    pairing(ctl).fail_next = [AccessoryNotFoundError("gone")]
    res = await bridge.read("hallway")
    assert not res.ok and not res.available


async def test_authentication_error_means_re_pair(tmp_path):
    bridge, ctl, _dev, _ = await loaded(tmp_path)
    pairing(ctl).fail_always = AuthenticationError("pairing removed")
    res = await bridge.read("hallway")
    assert not res.ok and res.needs_repair and res.error == hk.REPAIR_MSG
    assert bridge.needs_repair("hallway") and not bridge.is_available("hallway")


# --- subscribe / events ---------------------------------------------------------------


@pytest.mark.parametrize("vendor_ev", [False, True])
async def test_subscribes_only_ev_sensor_characteristics(tmp_path, vendor_ev):
    dev = FakeDevice(status_flags=0, vendor_ev=vendor_ev)
    bridge, ctl, dev, _ = await loaded(tmp_path, dev)
    p = pairing(ctl)
    p.reject_subscribe = {dev.k(BIG_AID, "BATTERY_LEVEL")}
    n = await bridge.subscribe("hallway")
    (chars,) = [payload for kind, payload in p.calls if kind == "subscribe"]
    keys = {(aid, iid): name for (aid, name), iid in dev.iids.items()}
    names = {keys[c] for c in chars}
    assert names == {"TEMPERATURE_CURRENT", "RELATIVE_HUMIDITY_CURRENT", "HEATING_COOLING_CURRENT",
                     "OCCUPANCY_DETECTED", "MOTION_DETECTED", "STATUS_ACTIVE", "STATUS_LO_BATT", "BATTERY_LEVEL"}
    assert dev.k(1, "VENDOR_ECOBEE_TIMESTAMP") not in chars  # never subscribe a write target
    assert n == len(chars) - 1


async def test_push_off_after_disconnect_recreates_the_pairing_object(tmp_path):
    bridge, ctl, dev, seen = await loaded(tmp_path)
    old = pairing(ctl)
    old.disconnect_on_subscribe = True
    assert await bridge.subscribe("hallway") == 0
    assert bridge.needs_recreate("hallway")
    await bridge.recreate("hallway")
    new = pairing(ctl)
    assert new is not old and old.shut_down and not old.listeners
    assert not bridge.needs_recreate("hallway")
    assert await bridge.subscribe("hallway") > 0
    old.push({dev.k(BIG_AID, "OCCUPANCY_DETECTED"): {"value": 1}})  # stale object: ignored
    assert seen == []
    new.push({dev.k(BIG_AID, "OCCUPANCY_DETECTED"): {"value": 1}})
    assert seen == [("hallway", {BIG_AID})]


async def test_events_update_values_and_ignore_write_echoes(tmp_path):
    bridge, ctl, dev, seen = await loaded(tmp_path)
    await bridge.subscribe("hallway")
    p = pairing(ctl)
    p.push({})  # (re)connect marker
    assert seen == [("hallway", set())]
    p.push({dev.k(BIG_AID, "OCCUPANCY_DETECTED"): {"value": 1}, dev.k(BIG_AID, "BATTERY_LEVEL"): {"status": -70402}})
    assert seen[-1] == ("hallway", {BIG_AID})
    assert bridge.values("hallway")[BIG_AID]["OCCUPANCY_DETECTED"] == 1
    seen.clear()
    until = datetime.now(UTC) + timedelta(minutes=90)
    result = await bridge.set_climate_hold("hallway", "away", until, TZ)
    assert result.ok
    assert seen == []  # the library's optimistic echo of TIMESTAMP is not device state


# --- writes ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["T", "Q", "R", ""])
async def test_climate_hold_reuses_suffix_in_two_separate_puts(tmp_path, suffix):
    dev = FakeDevice(status_flags=0, suffix=suffix)
    bridge, ctl, dev, _ = await loaded(tmp_path, dev)
    p = pairing(ctl)
    p.calls.clear()
    until = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=90)
    result = await bridge.set_climate_hold("hallway", "away", until, TZ)
    assert result.ok, result.error
    expected = until.astimezone(ZoneInfo(TZ)).isoformat(timespec="seconds") + suffix
    kinds = [kind for kind, _ in p.calls]
    assert kinds == ["get", "put", "put", "get"]
    first_get = p.calls[0][1]
    assert dev.k(1, "VENDOR_ECOBEE_TIMESTAMP") in first_get
    puts = [payload for kind, payload in p.calls if kind == "put"]
    ts_iid = dev.iids[(1, "VENDOR_ECOBEE_TIMESTAMP")]
    hold_iid = dev.iids[(1, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE")]
    assert puts == [[(1, ts_iid, expected)], [(1, hold_iid, 2)]]  # end time alone, then the hold
    assert result.readback == {"timestamp": expected, "current_mode": 2, "expected_timestamp": expected,
                               "expected_mode": 2}
    assert result.before["current_mode"] == 0 and result.channel == "homekit"


async def test_put_status_nonzero_is_a_failure_status_zero_is_not(tmp_path):
    bridge, ctl, dev, _ = await loaded(tmp_path)
    until = datetime.now(UTC) + timedelta(hours=1)
    ok = await bridge.set_climate_hold("hallway", "home", until, TZ)  # fake returns status-0 entries
    assert ok.ok
    pairing(ctl).put_status = {dev.k(1, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE"): -70410}
    bad = await bridge.set_climate_hold("hallway", "sleep", until, TZ)
    assert not bad.ok and "rejected" in (bad.error or "")
    assert bad.readback is None


async def test_readback_mismatch_is_not_verified(tmp_path):
    bridge, _ctl, dev, _ = await loaded(tmp_path)
    original = dev.apply_write

    def ignore_hold(aid, iid, value):
        if (aid, iid) != dev.k(1, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE"):
            original(aid, iid, value)

    dev.apply_write = ignore_hold
    res = await bridge.set_climate_hold("hallway", "away", datetime.now(UTC) + timedelta(hours=1), TZ)
    assert not res.ok and "current mode" in (res.error or "")


async def test_never_writes_home_sleep_away_targets(tmp_path):
    bridge, ctl, dev, _ = await loaded(tmp_path)
    await bridge.set_climate_hold("hallway", "sleep", datetime.now(UTC) + timedelta(hours=2), TZ)
    await bridge.clear_hold("hallway")
    written_types = {dev.types[(aid, iid)] for aid, iid, _ in dev.writes}
    assert written_types and not (written_types & hk.FORBIDDEN_WRITE_TYPES)
    p = pairing(ctl)
    p.calls.clear()
    ent = bridge._paired["hallway"]
    with pytest.raises(hk.HomekitWriteForbidden):
        await bridge._put(ent, [(1, dev.iids[(1, HOME_TARGET_HEAT)], 21.0)])
    with pytest.raises(hk.HomekitWriteForbidden):
        await bridge._put(ent, [(BIG_AID, dev.iids[(BIG_AID, "TEMPERATURE_CURRENT")], 21.0)])
    assert p.calls == []


async def test_clear_hold_reads_back(tmp_path):
    bridge, _ctl, dev, _ = await loaded(tmp_path)
    await bridge.set_climate_hold("hallway", "away", datetime.now(UTC) + timedelta(hours=1), TZ)
    res = await bridge.clear_hold("hallway")
    assert res.ok and res.readback["current_mode"] == 0
    assert dev.writes[-1] == (1, dev.iids[(1, "VENDOR_ECOBEE_CLEAR_HOLD")], True)


# --- pairing --------------------------------------------------------------------------


async def test_pairing_saves_before_closing_and_returns_keys(tmp_path):
    dev = FakeDevice()
    bridge, ctl, _ = make_bridge(tmp_path, [dev])
    await bridge.start()
    found = await bridge.begin_pairing("AA:BB:CC:DD:EE:01", "hallway")
    assert found.id == dev.id and found.unpaired
    assert bridge.has_pending(dev.id)
    saved = []

    async def save(data):
        dev.log.append("save")
        saved.append(data)

    data = await bridge.finish_pairing(dev.id, "123-45-678", save=save)
    assert dev.log == ["start:hallway", "finished", "save", "close_temporary"]
    assert data["iOSDeviceLTSK"] == SECRET_LTSK and saved == [data]
    assert "hallway" not in ctl.pairings  # the temporary alias-keyed pairing is dropped
    assert not bridge.has_pending(dev.id)


async def test_bad_code_format_keeps_the_session_wrong_code_ends_it(tmp_path):
    dev = FakeDevice()
    bridge, _ctl, _ = make_bridge(tmp_path, [dev])
    await bridge.start()
    await bridge.begin_pairing(dev.id, "hallway")
    with pytest.raises(hk.HomekitPairingError, match="123-45-678"):
        await bridge.finish_pairing(dev.id, "12345678", save=lambda d: None)
    assert bridge.has_pending(dev.id)
    with pytest.raises(hk.HomekitPairingError) as wrong:
        await bridge.finish_pairing(dev.id, "999-99-999", save=lambda d: None)
    assert str(wrong.value) == hk.WRONG_CODE_MSG
    assert not bridge.has_pending(dev.id)
    with pytest.raises(hk.HomekitPairingError) as gone:
        await bridge.finish_pairing(dev.id, "123-45-678", save=lambda d: None)
    assert str(gone.value) == hk.NO_PENDING_MSG


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        ("paired", "Disconnect from HomeKit"),
        (BusyError("busy"), "busy"),
        (MaxTriesError("tries"), "Too many wrong codes"),
        (MaxPeersError("peers"), "cannot accept another"),
        ("invisible", "not found on the network"),
    ],
)
async def test_pairing_errors_have_clear_messages(tmp_path, setup, needle):
    dev = FakeDevice()
    if setup == "paired":
        dev.paired = True
    elif setup == "invisible":
        dev.visible = False
    else:
        dev.start_error = setup
    bridge, _ctl, _ = make_bridge(tmp_path, [dev])
    await bridge.start()
    with pytest.raises(hk.HomekitPairingError, match=needle):
        await bridge.begin_pairing(dev.id, "hallway")
    assert not bridge.has_pending(dev.id)


async def test_failed_save_removes_the_new_pairing_from_the_thermostat(tmp_path):
    dev = FakeDevice()
    bridge, _ctl, _ = make_bridge(tmp_path, [dev])
    await bridge.start()
    await bridge.begin_pairing(dev.id, "hallway")

    def save(_data):
        raise RuntimeError("database down")

    with pytest.raises(hk.HomekitPairingError, match="removed from the thermostat"):
        await bridge.finish_pairing(dev.id, "123-45-678", save=save)
    assert "removed_on_device" in dev.log and dev.log[-1] == "close_temporary"
    assert not dev.paired


async def test_pairing_keys_are_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    dev = FakeDevice()
    bridge, ctl, _ = make_bridge(tmp_path, [dev])
    await bridge.start()
    await bridge.begin_pairing(dev.id, "hallway")
    data = await bridge.finish_pairing(dev.id, "123-45-678", save=lambda d: None)
    await bridge.load("hallway", data)
    await bridge.inventory("hallway")
    await bridge.subscribe("hallway")
    await bridge.read("hallway")
    pairing(ctl).fail_always = AuthenticationError("gone")
    await bridge.read("hallway")
    await bridge.stop()
    assert caplog.records
    assert SECRET_LTSK not in caplog.text


# --- unpair / discovery ---------------------------------------------------------------


async def test_remove_unpairs_on_the_device(tmp_path):
    bridge, ctl, dev, _ = await loaded(tmp_path)
    assert await bridge.remove("hallway") == "removed"
    assert not dev.paired and not bridge.is_loaded("hallway") and "hallway" not in ctl.aliases


async def test_remove_unreachable_raises_already_removed_is_ok(tmp_path):
    bridge, ctl, dev, _ = await loaded(tmp_path)
    pairing(ctl).fail_always = disconnected()
    with pytest.raises(hk.HomekitUnavailable, match="must be reachable"):
        await bridge.remove("hallway")
    assert dev.paired and not bridge.is_loaded("hallway")
    await bridge.load("hallway", dev.pairing_data())
    pairing(ctl).fail_always = AuthenticationError("unknown controller")
    assert await bridge.remove("hallway") == "already_removed"


async def test_discover_lists_devices(tmp_path):
    a = FakeDevice("aa:bb:cc:dd:ee:01", "Hallway")
    b = FakeDevice("aa:bb:cc:dd:ee:02", "Bedroom", status_flags=0)
    bridge, _ctl, _ = make_bridge(tmp_path, [a, b])
    await bridge.start()
    found = {d.id: d for d in await bridge.discover()}
    assert set(found) == {a.id, b.id}
    assert found[a.id].unpaired and not found[b.id].unpaired
    assert (found[a.id].name, found[a.id].model, found[a.id].port) == ("Hallway", "ECB501", 51826)

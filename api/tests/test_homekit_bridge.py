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
    assert hk.COMFORT_TARGET_TYPES == {
        "home": (CT.VENDOR_ECOBEE_HOME_TARGET_HEAT, CT.VENDOR_ECOBEE_HOME_TARGET_COOL),
        "sleep": (CT.VENDOR_ECOBEE_SLEEP_TARGET_HEAT, CT.VENDOR_ECOBEE_SLEEP_TARGET_COOL),
        "away": (CT.VENDOR_ECOBEE_AWAY_TARGET_HEAT, CT.VENDOR_ECOBEE_AWAY_TARGET_COOL),
    }
    # the comfort targets are read-only: every one of them is write-forbidden
    assert {t for pair in hk.COMFORT_TARGET_TYPES.values() for t in pair} == hk.FORBIDDEN_WRITE_TYPES
    read_only = {"HEATING_COOLING_TARGET", "TEMPERATURE_HEATING_THRESHOLD", "TEMPERATURE_COOLING_THRESHOLD"}
    assert read_only <= set(hk.CHAR_TYPES) and not (read_only & (hk.ALLOWED_WRITE_KEYS | hk.PUSH_KEYS))


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
    readable = 8 * 12 + 11  # 8 readable per sensor, 11 on the thermostat (write-only ones and the
    # comfort targets, read only before a climate hold, are skipped)
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


async def test_read_comfort_targets_is_one_get_and_never_a_put(tmp_path):
    bridge, ctl, dev, _ = await loaded(tmp_path)
    p = pairing(ctl)
    p.calls.clear()
    targets = await bridge.read_comfort_targets("hallway")
    assert targets == {"home": {"heat_f": 68.0, "cool_f": 76.0}, "sleep": {"heat_f": 67.0, "cool_f": 74.0},
                       "away": {"heat_f": 62.0, "cool_f": 80.0}}
    assert [kind for kind, _ in p.calls] == ["get"]
    assert {dev.types[k] for k in p.calls[0][1]} == hk.FORBIDDEN_WRITE_TYPES
    assert dev.writes == []
    # one the thermostat does not answer for stays unknown
    p.missing = {dev.k(1, "05B97374-6DC0-439B-A0FA-CA33F612D425")}  # sleep heat
    assert (await bridge.read_comfort_targets("hallway"))["sleep"] == {"heat_f": None, "cool_f": 74.0}
    p.fail_next = [disconnected()]
    with pytest.raises(hk.HomekitError):
        await bridge.read_comfort_targets("hallway")


# --- the HomeKit fallback snapshot (pure) ---------------------------------------------


SNAP_NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)  # 15:00 in Chicago
AID_MAP = {1: "main.hallway_tstat", BIG_AID: "main.kitchen"}


def tstat_values(**over) -> dict:
    vals = {
        "TEMPERATURE_CURRENT": 22.0, "RELATIVE_HUMIDITY_CURRENT": 45.0, "HEATING_COOLING_CURRENT": 2,
        "HEATING_COOLING_TARGET": 3, "TEMPERATURE_HEATING_THRESHOLD": 20.0, "TEMPERATURE_COOLING_THRESHOLD": 24.4,
        "VENDOR_ECOBEE_CURRENT_MODE": 0, "VENDOR_ECOBEE_TIMESTAMP": "2026-10-04T22:00:00-05:00R",
        "OCCUPANCY_DETECTED": 1,
    }
    vals.update(over)
    return {1: vals, BIG_AID: {"TEMPERATURE_CURRENT": 21.5, "OCCUPANCY_DETECTED": 0, "STATUS_ACTIVE": True}}


def cloud_snapshot(hold=None):
    return hk.UnitSnapshot(
        unit_key="main", ts=SNAP_NOW - timedelta(minutes=20), source="ecobee", name="Hallway", model="aresSmart",
        hvac_mode="cool", heat_sp_f=68.0, cool_sp_f=75.0, climate_ref="home", hold=hold, outdoor_temp_f=84.0,
        sensor_sets={"home": ["main.hallway_tstat"]},
        settings={"heatCoolMinDelta": 4.0, "hasHeatPump": False, "program_heat_f": 68.0, "program_cool_f": 76.0},
    )


def test_homekit_snapshot_reads_the_thermostat_and_carries_only_configuration():
    snap = hk.snapshot_from_values("main", tstat_values(), AID_MAP, SNAP_NOW, TZ, previous=cloud_snapshot())
    assert (snap.source, snap.ts, snap.connected, snap.revision) == ("homekit", SNAP_NOW, True, None)
    assert (snap.zone_temp_f, snap.zone_humidity) == (71.6, 45.0)
    assert (snap.hvac_mode, snap.heat_sp_f, snap.cool_sp_f) == ("auto", 68.0, 76.0)
    assert snap.equipment_running == ["compCool1"] and snap.climate_ref == "home" and snap.hold is None
    assert {r.sensor_key: r.temp_f for r in snap.sensors} == {"main.hallway_tstat": 71.6, "main.kitchen": 70.7}
    assert (snap.name, snap.model, snap.sensor_sets) == ("Hallway", "aresSmart", {"home": ["main.hallway_tstat"]})
    assert snap.settings == {"heatCoolMinDelta": 4.0, "hasHeatPump": False}  # no stale program setpoints
    assert snap.outdoor_temp_f is None  # not read over HomeKit: never invented
    heating = hk.snapshot_from_values("main", tstat_values(HEATING_COOLING_CURRENT=1, HEATING_COOLING_TARGET=None),
                                      AID_MAP, SNAP_NOW, TZ, previous=cloud_snapshot())
    assert heating.equipment_running == ["auxHeat1"] and heating.hvac_mode == "cool"  # mode kept from the cloud
    bare = hk.snapshot_from_values("main", {1: {"TEMPERATURE_CURRENT": 22.0}}, AID_MAP, SNAP_NOW, TZ)
    assert (bare.hvac_mode, bare.climate_ref, bare.hold, bare.equipment_running) == ("off", None, None, [])


def test_homekit_snapshot_hold_rules():
    until = SNAP_NOW + timedelta(minutes=90)
    stamp = hk.format_hold_end(until, TZ, "R")
    # 1. our verified climate hold, shown by CURRENT_MODE and TIMESTAMP
    ours = hk.snapshot_from_values("main", tstat_values(VENDOR_ECOBEE_CURRENT_MODE=2, VENDOR_ECOBEE_TIMESTAMP=stamp),
                                   AID_MAP, SNAP_NOW, TZ, our_hold=("away", until))
    assert ours.hold is not None
    assert (ours.hold.kind, ours.hold.climate_ref, ours.hold.end, ours.hold.set_by_us) == ("climate", "away", until, True)
    # ...but not when the thermostat shows another climate or another end time
    for over in ({"VENDOR_ECOBEE_CURRENT_MODE": 0, "VENDOR_ECOBEE_TIMESTAMP": stamp},
                 {"VENDOR_ECOBEE_CURRENT_MODE": 2, "VENDOR_ECOBEE_TIMESTAMP": "2026-10-04T22:00:00-05:00R"}):
        other = hk.snapshot_from_values("main", tstat_values(**over), AID_MAP, SNAP_NOW, TZ, our_hold=("away", until))
        assert other.hold is None
    # 2. the cloud's record of OUR temperature hold is carried while the thermostat still shows it
    cloud_hold = hk.HoldInfo(kind="temperature", heat_f=68.0, cool_f=76.0, end=until, hold_type="holdHours",
                             set_by_us=True)
    temp = tstat_values(VENDOR_ECOBEE_CURRENT_MODE=3, VENDOR_ECOBEE_TIMESTAMP=stamp)
    carried = hk.snapshot_from_values("main", temp, AID_MAP, SNAP_NOW, TZ, previous=cloud_snapshot(cloud_hold))
    assert carried.hold == cloud_hold
    # 3. ...changed by hand (other setpoints): a temperature hold that is NOT ours
    moved = tstat_values(VENDOR_ECOBEE_CURRENT_MODE=3, VENDOR_ECOBEE_TIMESTAMP=stamp,
                         TEMPERATURE_COOLING_THRESHOLD=22.2)
    manual = hk.snapshot_from_values("main", moved, AID_MAP, SNAP_NOW, TZ, previous=cloud_snapshot(cloud_hold))
    assert manual.hold is not None and manual.hold.set_by_us is False
    assert (manual.hold.kind, manual.hold.heat_f, manual.hold.cool_f, manual.hold.end) == ("temperature", 68.0, 72.0, until)
    # 4. a cloud hold that ended, or a mode that no longer shows it: no hold
    ended = cloud_hold.model_copy(update={"end": SNAP_NOW - timedelta(minutes=1)})
    assert hk.snapshot_from_values("main", tstat_values(), AID_MAP, SNAP_NOW, TZ,
                                   previous=cloud_snapshot(ended)).hold is None
    assert hk.snapshot_from_values("main", tstat_values(), AID_MAP, SNAP_NOW, TZ,
                                   previous=cloud_snapshot(cloud_hold)).hold is None
    # a vacation with a known end is kept until that end, whatever the mode shows
    vac = hk.HoldInfo(kind="temperature", heat_f=62.0, cool_f=82.0, end=SNAP_NOW + timedelta(days=2),
                      hold_type="vacation")
    kept = hk.snapshot_from_values("main", tstat_values(), AID_MAP, SNAP_NOW, TZ, previous=cloud_snapshot(vac))
    assert kept.hold is not None and kept.hold.hold_type == "vacation" and kept.hold.set_by_us is False


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

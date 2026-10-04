"""HomeKit (HAP over IP) adapter: the server is the HomeKit controller for each ecobee
(aiohomekit 4.0.1; verified notes in docs/HOMEKIT.md). No Apple device, Apple Home,
Homebridge or Home Assistant. Runs ONLY inside the host-networked homekit service
(``climate.collector.homekit_service``) because discovery and pairing need mDNS.

Rules: one AsyncZeroconf + one AsyncServiceBrowser for BOTH '_hap._tcp.local.' and
'_hap._udp.local.' kept for the process lifetime; ``controller.load_pairing`` only after the
controller started; never use ``aiohomekitctl pair`` (it loses the keys); pairing data is
saved encrypted in the DB (secrets 'homekit_pairing:<alias>'), never to a plain file.
Vendor characteristics on current firmware are 'pr' without 'ev': poll them. Subscribe only
'ev' characteristics; check ``supports_subscribe``. put_characteristics: an entry is a
failure only when status != 0. Writes: TIMESTAMP (reuse the unit's suffix) in its own put,
then SET_HOLD_SCHEDULE in a separate put, then read back. Never write HOME/SLEEP/AWAY targets
(they are only ever READ, before a climate hold, to check what that hold would hold).
``snapshot_from_values`` turns a poll into a UnitSnapshot for when the ecobee cloud is down.

This module is a pure adapter: no database access. aiohomekit is imported lazily so the rest
of the stack (which does not install the ``homekit`` extra) can import these helpers.
Tests inject a fake controller through ``HomekitBridge(controller_factory=...)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from climate.house import ROOM_BY_KEY, SENSORS, normalize_name
from climate.sources.base import HoldInfo, HvacMode, SensorReading, UnitSnapshot, WriteResult

log = logging.getLogger("climate.homekit")

HAP_TYPES = ("_hap._tcp.local.", "_hap._udp.local.")
CODE_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")
MAX_BATCH = 49  # characteristics per GET (Home Assistant's limit)
FAILS_UNTIL_UNAVAILABLE = 3
FIND_TIMEOUT_S = 30.0
THERMOSTAT_AID = 1
STATUS_UNPAIRED = 0x01

ClimateName = Literal["home", "sleep", "away"]
CLIMATE_CODES: dict[str, int] = {"home": 0, "sleep": 1, "away": 2}  # SET_HOLD_SCHEDULE values
# VENDOR_ECOBEE_CURRENT_MODE readings: home 0, sleep 1, away 2, temp 3.
MODE_CLIMATES: dict[int, str] = {code: name for name, code in CLIMATE_CODES.items()}
MODE_TEMPERATURE_HOLD = 3
HVAC_TARGETS: dict[int, str] = {0: "off", 1: "heat", 2: "cool", 3: "auto"}  # HEATING_COOLING_TARGET
OUR_HOLD_MATCH_S = 120  # TIMESTAMP within this of our hold's end = our hold is the one running

_BASE_UUID = "-0000-1000-8000-0026BB765291"

# Characteristic types we read or write, keyed by aiohomekit's CharacteristicsTypes names
# (aiohomekit 4.0.1; tests/test_homekit_bridge.py asserts these equal the library's values).
CHAR_TYPES: dict[str, str] = {
    "TEMPERATURE_CURRENT": "00000011" + _BASE_UUID,
    "RELATIVE_HUMIDITY_CURRENT": "00000010" + _BASE_UUID,
    "OCCUPANCY_DETECTED": "00000071" + _BASE_UUID,
    "MOTION_DETECTED": "00000022" + _BASE_UUID,
    "VENDOR_ECOBEE_OCCUPANCY_LAST_ACTIVATION": "A8F798E0-4A40-11E6-BDF4-0800200C9A66",
    "VENDOR_ECOBEE_MOTION_LAST_ACTIVATION": "BFE61C70-4A40-11E6-BDF4-0800200C9A66",
    "STATUS_ACTIVE": "00000075" + _BASE_UUID,
    "STATUS_LO_BATT": "00000079" + _BASE_UUID,
    "BATTERY_LEVEL": "00000068" + _BASE_UUID,
    "HEATING_COOLING_CURRENT": "0000000F" + _BASE_UUID,
    # Read only (polled for the HomeKit fallback snapshot; never written, never subscribed):
    "HEATING_COOLING_TARGET": "00000033" + _BASE_UUID,  # 0 off, 1 heat, 2 cool, 3 auto
    "TEMPERATURE_HEATING_THRESHOLD": "00000012" + _BASE_UUID,  # active heat setpoint, °C
    "TEMPERATURE_COOLING_THRESHOLD": "0000000D" + _BASE_UUID,  # active cool setpoint, °C
    "VENDOR_ECOBEE_EQUIPMENT_RUNNING": "4A6AE4F6-036C-495D-87CC-B3702B437741",
    "VENDOR_ECOBEE_CURRENT_MODE": "B7DDB9A3-54BB-4572-91D2-F1F5B0510F8C",
    "VENDOR_ECOBEE_TIMESTAMP": "1621F556-1367-443C-AF19-82AF018E99DE",
    "VENDOR_ECOBEE_SET_HOLD_SCHEDULE": "1B300BC2-CFFC-47FF-89F9-BD6CCF5F2853",
    "VENDOR_ECOBEE_CLEAR_HOLD": "FA128DE6-9D7D-49A4-B6D8-4E4E234DEE38",
}
KEY_BY_TYPE: dict[str, str] = {v: k for k, v in CHAR_TYPES.items()}

# Accessory information (name / model / firmware) and the thermostat service.
INFO_TYPES: dict[str, str] = {
    "NAME": "00000023" + _BASE_UUID,
    "MODEL": "00000021" + _BASE_UUID,
    "FIRMWARE_REVISION": "00000052" + _BASE_UUID,
}
SERVICE_ACCESSORY_INFORMATION = "0000003E" + _BASE_UUID
SERVICE_THERMOSTAT = "0000004A" + _BASE_UUID

# Writing any of these permanently edits the thermostat's comfort settings. NEVER write them.
FORBIDDEN_WRITE_TYPES: frozenset[str] = frozenset(
    {
        "E4489BBC-5227-4569-93E5-B345E3E5508F",  # VENDOR_ECOBEE_HOME_TARGET_HEAT
        "7D381BAA-20F9-40E5-9BE9-AEB92D4BECEF",  # VENDOR_ECOBEE_HOME_TARGET_COOL
        "05B97374-6DC0-439B-A0FA-CA33F612D425",  # VENDOR_ECOBEE_SLEEP_TARGET_HEAT
        "A251F6E7-AC46-4190-9C5D-3D06277BDF9F",  # VENDOR_ECOBEE_SLEEP_TARGET_COOL
        "73AAB542-892A-4439-879A-D2A883724B69",  # VENDOR_ECOBEE_AWAY_TARGET_HEAT
        "5DA985F0-898A-4850-B987-B76C6C78D670",  # VENDOR_ECOBEE_AWAY_TARGET_COOL
    }
)
# The same six, per comfort setting: READ ONLY, read just before a climate hold to check what
# that hold would hold (a climate hold holds whatever the schedule's setpoints are).
COMFORT_TARGET_TYPES: dict[str, tuple[str, str]] = {
    "home": ("E4489BBC-5227-4569-93E5-B345E3E5508F", "7D381BAA-20F9-40E5-9BE9-AEB92D4BECEF"),
    "sleep": ("05B97374-6DC0-439B-A0FA-CA33F612D425", "A251F6E7-AC46-4190-9C5D-3D06277BDF9F"),
    "away": ("73AAB542-892A-4439-879A-D2A883724B69", "5DA985F0-898A-4850-B987-B76C6C78D670"),
}
# The only characteristics this adapter ever writes.
ALLOWED_WRITE_KEYS: frozenset[str] = frozenset(
    {"VENDOR_ECOBEE_TIMESTAMP", "VENDOR_ECOBEE_SET_HOLD_SCHEDULE", "VENDOR_ECOBEE_CLEAR_HOLD"}
)
WRITE_ONLY_KEYS: frozenset[str] = frozenset({"VENDOR_ECOBEE_SET_HOLD_SCHEDULE", "VENDOR_ECOBEE_CLEAR_HOLD"})
# Standard characteristics worth push events. Vendor counters change every second (or have no
# 'ev' on current firmware) and are polled; write targets are never subscribed, so the
# library's optimistic echo of our own writes can never be mistaken for device state.
PUSH_KEYS: frozenset[str] = frozenset(
    {
        "TEMPERATURE_CURRENT",
        "RELATIVE_HUMIDITY_CURRENT",
        "OCCUPANCY_DETECTED",
        "MOTION_DETECTED",
        "HEATING_COOLING_CURRENT",
        "STATUS_ACTIVE",
        "STATUS_LO_BATT",
        "BATTERY_LEVEL",
    }
)

# Implausible indoor readings (an ecobee can report stale values such as 100 °C right after a
# restart) are dropped rather than stored: never invent a temperature.
TEMP_C_RANGE = (-30.0, 60.0)

ALREADY_PAIRED_MSG = (
    "This thermostat is already paired to another HomeKit controller (Apple Home, Homebridge, "
    "Home Assistant...). On the thermostat choose Disconnect from HomeKit (under Settings > Reset), "
    "wait until it shows here as unpaired, then pair again."
)
WRONG_CODE_MSG = "Wrong code. Start pairing again: the thermostat shows a new code for each attempt."
BAD_CODE_FORMAT_MSG = "The code must look like 123-45-678 (8 digits with dashes)."
NO_PENDING_MSG = "No pairing is in progress for this thermostat (it timed out or the service restarted). Start again."
REPAIR_MSG = (
    "The thermostat no longer recognizes this server's HomeKit pairing (it was removed or HomeKit was "
    "reset on the device). Unpair it here, then pair again."
)


# ---------------------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------------------


class HomekitError(Exception):
    """Base class; the message is safe to show the owner (never contains key material)."""


class HomekitPairingError(HomekitError):
    """Pair-setup failed; message explains what the owner should do."""


class HomekitUnavailable(HomekitError):
    """The thermostat could not be reached (network, mDNS, timeouts)."""


class HomekitAuthError(HomekitError):
    """The device rejected our keys: the pairing was removed on the device. Needs a re-pair."""


class HomekitWriteError(HomekitError):
    """A put returned a non-zero status, or a write precondition failed."""


class HomekitWriteForbidden(HomekitWriteError):
    """Attempt to write a characteristic outside ALLOWED_WRITE_KEYS (a programming error)."""


def _exc() -> Any:
    """aiohomekit.exceptions, imported lazily (the homekit extra is optional)."""
    from aiohomekit import exceptions

    return exceptions


def _is(exc: BaseException, *names: str) -> bool:
    try:
        mod = _exc()
    except ImportError:  # pragma: no cover - only without the homekit extra
        return False
    classes = tuple(getattr(mod, n) for n in names if hasattr(mod, n))
    return bool(classes) and isinstance(exc, classes)


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text[:160]}" if text else name


def pairing_error_message(exc: BaseException) -> str:
    """Owner-facing message for a pair-setup failure."""
    if isinstance(exc, HomekitError):
        return str(exc)
    if _is(exc, "UnavailableError", "AlreadyPairedError"):
        return ALREADY_PAIRED_MSG
    if _is(exc, "AuthenticationError"):
        return WRONG_CODE_MSG
    if _is(exc, "MalformedPinError"):
        return BAD_CODE_FORMAT_MSG
    if _is(exc, "BusyError"):
        return "The thermostat is busy with another pairing attempt. Wait a minute and try again."
    if _is(exc, "MaxTriesError"):
        return (
            "Too many wrong codes: the thermostat refuses pairing now. Restart it (or reset HomeKit on it), "
            "then try again."
        )
    if _is(exc, "MaxPeersError"):
        return (
            "The thermostat cannot accept another HomeKit controller. Choose Disconnect from HomeKit on the "
            "thermostat, then try again."
        )
    if _is(exc, "BackoffError"):
        return "The thermostat asked us to wait before another pairing attempt. Try again in a minute."
    if _is(exc, "AccessoryNotFoundError"):
        return (
            "Thermostat not found on the network. Check it is powered and on the same LAN/VLAN as the server "
            "(mDNS), and that it shows in the device list."
        )
    if _is(exc, "AccessoryDisconnectedError", "EncryptionError") or isinstance(exc, (OSError, asyncio.TimeoutError)):
        return f"Could not talk to the thermostat ({_short(exc)}). Check the network and try again."
    return f"Pairing failed ({_short(exc)})."


# ---------------------------------------------------------------------------------------
# pure helpers: inventory parsing, aid mapping, readings, timestamps
# ---------------------------------------------------------------------------------------


def normalize_type(value: str) -> str:
    """aiohomekit's UUID normalization: upper case, short HAP types expanded."""
    v = str(value).upper()
    if len(v) <= 8:
        return v.rjust(8, "0") + _BASE_UUID
    return v


@dataclass
class ParsedAccessories:
    inventory: list[dict[str, Any]]  # [{aid, name, model, firmware, chars: {key: {iid, perms, type}}}]
    index: dict[tuple[int, int], str]  # (aid, iid) -> our key, for the characteristics we use
    types: dict[tuple[int, int], str]  # (aid, iid) -> type, for EVERY characteristic (write guard)
    perms: dict[tuple[int, int], list[str]]


def parse_accessories(raw: Iterable[Mapping[str, Any]]) -> ParsedAccessories:
    """Parse the raw /accessories list. Aids are kept as Python ints (they can exceed 2^32).

    When a characteristic type appears in several services of one accessory, the thermostat
    service wins, then the first occurrence."""
    inventory: list[dict[str, Any]] = []
    index: dict[tuple[int, int], str] = {}
    types: dict[tuple[int, int], str] = {}
    perms_by: dict[tuple[int, int], list[str]] = {}
    for acc in raw:
        aid = int(acc["aid"])
        services = list(acc.get("services") or [])
        services.sort(key=lambda s: 0 if normalize_type(s.get("type", "")) == SERVICE_THERMOSTAT else 1)
        info: dict[str, Any] = {}
        chars: dict[str, dict[str, Any]] = {}
        for svc in services:
            stype = normalize_type(svc.get("type", ""))
            for ch in svc.get("characteristics") or []:
                if "iid" not in ch or "type" not in ch:
                    continue
                iid = int(ch["iid"])
                ctype = normalize_type(ch["type"])
                perms = [str(p) for p in (ch.get("perms") or [])]
                types[(aid, iid)] = ctype
                perms_by[(aid, iid)] = perms
                if stype == SERVICE_ACCESSORY_INFORMATION:
                    for name, t in INFO_TYPES.items():
                        if ctype == t and name not in info:
                            info[name] = ch.get("value")
                key = KEY_BY_TYPE.get(ctype)
                if key is not None and key not in chars:
                    chars[key] = {"iid": iid, "perms": perms, "type": ctype}
                    index[(aid, iid)] = key
        inventory.append(
            {
                "aid": aid,
                "name": _clean_str(info.get("NAME")),
                "model": _clean_str(info.get("MODEL")),
                "firmware": _clean_str(info.get("FIRMWARE_REVISION")),
                "chars": chars,
            }
        )
    inventory.sort(key=lambda a: a["aid"])
    return ParsedAccessories(inventory=inventory, index=index, types=types, perms=perms_by)


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


@dataclass(frozen=True)
class SensorRef:
    key: str
    name: str
    kind: str  # 'thermostat' | 'smartsensor'
    room_name: str


def default_sensor_refs(unit_key: str) -> list[SensorRef]:
    return [
        SensorRef(s.key, s.name, s.kind, ROOM_BY_KEY[s.room_key].name if s.room_key in ROOM_BY_KEY else s.name)
        for s in SENSORS
        if s.unit_key == unit_key
    ]


def build_aid_map(
    inventory: Iterable[Mapping[str, Any]],
    unit_key: str,
    existing: Mapping[str, int | None] | None = None,
    sensors: Iterable[SensorRef] | None = None,
) -> dict[int, str]:
    """Map this pairing's accessories to our sensor keys (scoped to ONE unit).

    1. ``existing`` mappings (sensor_key -> aid, e.g. set by the owner) win when that aid is
       present in the inventory.
    2. aid 1 is the thermostat -> the unit's thermostat sensor ('<unit>.<room>_tstat').
    3. other aids by name: ``normalize_name`` of the accessory name against the sensor names,
       then (if unambiguous) against the sensor's room name.
    Aids are never assumed to be 2..N (newer SmartSensors use aids above 2^32)."""
    refs = list(sensors) if sensors is not None else default_sensor_refs(unit_key)
    accs = {int(a["aid"]): a for a in inventory}
    result: dict[int, str] = {}
    taken: set[str] = set()
    for ref in refs:
        aid = (existing or {}).get(ref.key)
        if aid is not None and int(aid) in accs and int(aid) not in result:
            result[int(aid)] = ref.key
            taken.add(ref.key)
    tstat = next((r for r in refs if r.kind == "thermostat"), None)
    if tstat is not None and THERMOSTAT_AID in accs and THERMOSTAT_AID not in result and tstat.key not in taken:
        result[THERMOSTAT_AID] = tstat.key
        taken.add(tstat.key)
    remotes = [r for r in refs if r.kind != "thermostat"]
    by_name: dict[str, SensorRef] = {}
    for r in remotes:
        by_name.setdefault(normalize_name(r.name), r)
    by_room: dict[str, list[SensorRef]] = {}
    for r in remotes:
        by_room.setdefault(normalize_name(r.room_name), []).append(r)
    for aid in sorted(accs):
        if aid == THERMOSTAT_AID or aid in result:
            continue
        name = normalize_name(str(accs[aid].get("name") or ""))
        if not name:
            continue
        cand = by_name.get(name)
        if cand is None and len(by_room.get(name, [])) == 1:
            cand = by_room[name][0]
        if cand is not None and cand.key not in taken:
            result[aid] = cand.key
            taken.add(cand.key)
    return result


def _c_to_f(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not (TEMP_C_RANGE[0] <= float(v) <= TEMP_C_RANGE[1]):
        return None
    return round(float(v) * 9.0 / 5.0 + 32.0, 1)


def _num(v: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if (lo is not None and f < lo) or (hi is not None and f > hi):
        return None
    return f


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _flag(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return None


def reading_from_values(sensor_key: str, vals: Mapping[str, Any], ts: datetime) -> SensorReading | None:
    """One accessory's characteristic values (by our keys) -> SensorReading.

    HomeKit temperatures are °C; we store °F. seconds_since_* keep ecobee's -1 = never.
    Returns None when nothing usable is known."""
    temp_f = _c_to_f(vals.get("TEMPERATURE_CURRENT"))
    humidity = _num(vals.get("RELATIVE_HUMIDITY_CURRENT"), 0, 100)
    occupied = _flag(vals.get("OCCUPANCY_DETECTED"))
    motion = _flag(vals.get("MOTION_DETECTED"))
    s_motion = _int(vals.get("VENDOR_ECOBEE_MOTION_LAST_ACTIVATION"))
    s_occ = _int(vals.get("VENDOR_ECOBEE_OCCUPANCY_LAST_ACTIVATION"))
    active = _flag(vals.get("STATUS_ACTIVE"))
    battery_low = _flag(vals.get("STATUS_LO_BATT"))
    online = True if active is None else active
    if all(v is None for v in (temp_f, humidity, occupied, motion, s_motion, s_occ, battery_low)) and online:
        return None
    return SensorReading(
        sensor_key=sensor_key,
        ts=ts,
        temp_f=temp_f,
        humidity=None if humidity is None else round(humidity, 1),
        occupied=occupied,
        motion=motion,
        seconds_since_motion=s_motion,
        seconds_since_occupancy=s_occ,
        online=online,
        battery_low=battery_low,
    )


def readings_from_values(
    values: Mapping[int, Mapping[str, Any]],
    aid_map: Mapping[int, str],
    ts: datetime,
    aids: Iterable[int] | None = None,
) -> list[SensorReading]:
    """Per-aid values -> SensorReadings for the mapped sensors (optionally only ``aids``)."""
    wanted = set(aids) if aids is not None else set(values)
    out: list[SensorReading] = []
    for aid in sorted(wanted):
        key = aid_map.get(aid)
        if key is None or aid not in values:
            continue
        r = reading_from_values(key, values[aid], ts)
        if r is not None:
            out.append(r)
    return out


def setpoint_f(v: Any) -> float | None:
    """A HomeKit setpoint (°C) -> °F on ecobee's 0.5 °F grid (HomeKit rounds °C to 0.1, which
    would otherwise turn 68.5 °F into 68.54)."""
    f = _c_to_f(v)
    return None if f is None else round(f * 2) / 2


_SETPOINT_MATCH_F = 0.3
_HEAT_EQUIPMENT = {True: "heatPump", False: "auxHeat1"}  # by settings['hasHeatPump']


def _hold_still_shown(prev: HoldInfo, mode: int | None, heat: float | None, cool: float | None,
                      ts: datetime) -> bool:
    """Does the thermostat still show ``prev`` (a hold from the last live snapshot)?"""
    if prev.end is not None and prev.end <= ts:
        return False
    if prev.hold_type in ("vacation", "demandResponse") and prev.end is not None:
        return True  # scheduled events with a known end: kept until that end
    if mode is None:
        return False
    if prev.kind == "climate" or prev.climate_ref:
        return MODE_CLIMATES.get(mode) == (prev.climate_ref or "").lower()
    if mode != MODE_TEMPERATURE_HOLD:
        return False
    for held, now in ((prev.heat_f, heat), (prev.cool_f, cool)):
        if held is not None and now is not None and abs(held - now) > _SETPOINT_MATCH_F:
            return False
    return True


def snapshot_from_values(
    unit_key: str,
    values: Mapping[int, Mapping[str, Any]],
    aid_map: Mapping[int, str],
    ts: datetime,
    tz: str,
    *,
    previous: UnitSnapshot | None = None,
    our_hold: tuple[str, datetime] | None = None,
) -> UnitSnapshot:
    """A UnitSnapshot (source 'homekit') from one HomeKit poll, for when the cloud is down.

    Read from the thermostat (aid 1): zone temperature / humidity, the heating/cooling call
    (-> ``equipment_running``), the target mode (-> ``hvac_mode``), the active heat/cool
    thresholds (-> setpoints), CURRENT_MODE (-> ``climate_ref`` home/sleep/away). Sensors are
    the readings of every mapped accessory (thermostat + paired SmartSensors). Nothing is
    invented: what HomeKit does not show stays None. From ``previous`` (the last live snapshot)
    only configuration is carried: name, model, sensor sets, settings WITHOUT the schedule's
    ``program_*`` setpoints (the schedule may have moved on), and ``hvac_mode`` when HomeKit
    does not expose its target mode.

    Hold, first rule that applies (TIMESTAMP is ecobee's "next scheduled change": a hold's end,
    or the next schedule transition when no hold runs):
    1. ``our_hold`` = (climate, until) of our last verified HomeKit climate hold: still before
       ``until``, CURRENT_MODE shows that climate and TIMESTAMP (when readable) is within 2 min
       of ``until`` -> that climate hold, ``set_by_us``.
    2. ``previous.hold`` the thermostat still shows (not ended; climate hold while CURRENT_MODE
       is that climate; temperature hold while CURRENT_MODE is 3 and the thresholds still match;
       a vacation / demand response until its end) -> carried as it was.
    3. CURRENT_MODE 3 -> a temperature hold at the current thresholds, ending at TIMESTAMP when
       that is in the future; not ours (the cloud channel's own holds are caught by rule 2).
    4. otherwise no hold: HomeKit cannot tell a hand-set climate hold from the schedule."""
    tstat = values.get(THERMOSTAT_AID, {})
    prev_settings = dict(previous.settings) if previous is not None else {}
    mode = _int(tstat.get("VENDOR_ECOBEE_CURRENT_MODE"))
    climate = MODE_CLIMATES.get(mode) if mode is not None else None
    heat_sp = setpoint_f(tstat.get("TEMPERATURE_HEATING_THRESHOLD"))
    cool_sp = setpoint_f(tstat.get("TEMPERATURE_COOLING_THRESHOLD"))
    hvac: HvacMode | None = None
    target = _int(tstat.get("HEATING_COOLING_TARGET"))
    if target is not None and target in HVAC_TARGETS:
        hvac = HVAC_TARGETS[target]  # type: ignore[assignment]
    elif previous is not None:
        hvac = previous.hvac_mode
    call = _int(tstat.get("HEATING_COOLING_CURRENT"))
    equipment: list[str] = []
    if call == 1:
        equipment = [_HEAT_EQUIPMENT[prev_settings.get("hasHeatPump") is True]]
    elif call == 2:
        equipment = ["compCool1"]

    end: datetime | None = None
    raw_ts = tstat.get("VENDOR_ECOBEE_TIMESTAMP")
    if isinstance(raw_ts, str):
        try:
            end = parse_timestamp(raw_ts, tz)
        except (HomekitError, ValueError):
            end = None
    future_end = end if end is not None and end > ts else None

    hold: HoldInfo | None = None
    if our_hold is not None:
        ours_climate, until = our_hold
        if (until > ts and climate == ours_climate
                and (end is None or abs((end - until).total_seconds()) <= OUR_HOLD_MATCH_S)):
            hold = HoldInfo(kind="climate", climate_ref=climate, end=until, hold_type="dateTime", set_by_us=True)
    if hold is None and previous is not None and previous.hold is not None \
            and _hold_still_shown(previous.hold, mode, heat_sp, cool_sp, ts):
        hold = previous.hold.model_copy()
    if hold is None and mode == MODE_TEMPERATURE_HOLD:
        hold = HoldInfo(kind="temperature", heat_f=heat_sp, cool_f=cool_sp, end=future_end,
                        hold_type="dateTime" if future_end is not None else None, set_by_us=False)

    return UnitSnapshot(
        unit_key=unit_key,
        ts=ts,
        source="homekit",
        revision=None,
        name=previous.name if previous is not None else None,
        model=previous.model if previous is not None else None,
        hvac_mode=hvac or "off",
        equipment_running=equipment,
        heat_sp_f=heat_sp,
        cool_sp_f=cool_sp,
        climate_ref=climate,
        hold=hold,
        zone_temp_f=_c_to_f(tstat.get("TEMPERATURE_CURRENT")),
        zone_humidity=_num(tstat.get("RELATIVE_HUMIDITY_CURRENT"), 0, 100),
        sensors=readings_from_values(values, aid_map, ts),
        sensor_sets=dict(previous.sensor_sets) if previous is not None else {},
        settings={k: v for k, v in prev_settings.items() if k not in ("program_heat_f", "program_cool_f")},
        connected=True,
    )


_TS_RE = re.compile(
    r"^(?P<dt>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?P<off>Z|[+-]\d{2}:?\d{2})?(?P<suffix>[A-Za-z]?)$"
)


def split_timestamp(value: str) -> tuple[str, str | None, str]:
    """'2025-04-06T23:30:00-05:00R' -> ('2025-04-06T23:30:00', '-05:00', 'R')."""
    m = _TS_RE.match(value.strip())
    if not m:
        raise HomekitWriteError(f"unrecognised VENDOR_ECOBEE_TIMESTAMP value {value!r}")
    return m.group("dt"), m.group("off"), m.group("suffix")


def timestamp_suffix(value: str) -> str:
    """The unit's trailing letter ('T', 'Q', 'R' seen; '' on old firmware). Never hard-coded."""
    return split_timestamp(value)[2]


def parse_timestamp(value: str, tz: str) -> datetime:
    dt_s, off, _ = split_timestamp(value)
    if off is None:
        return datetime.fromisoformat(dt_s).replace(tzinfo=ZoneInfo(tz))
    if off == "Z":
        off = "+00:00"
    elif ":" not in off:
        off = off[:3] + ":" + off[3:]
    return datetime.fromisoformat(dt_s + off)


def format_hold_end(until: datetime, tz: str, suffix: str) -> str:
    """ISO-8601 local time with UTC offset plus the unit's suffix, e.g. '2026-10-04T15:30:00-05:00T'."""
    if until.tzinfo is None:
        raise ValueError("hold end must be timezone-aware")
    local = until.astimezone(ZoneInfo(tz)).replace(microsecond=0)
    return local.isoformat(timespec="seconds") + suffix


def ensure_state_dir(path: Path) -> Path:
    """Create the HomeKit state dir with mode 0700 (charmap cache only; never keys)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:  # pragma: no cover - depends on the host
        log.warning("cannot set mode 0700 on %s: %s", path, exc)
    charmap = path / "charmap.json"
    if not charmap.exists():
        fd = os.open(charmap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write('{"pairings": {}}')
    return path


# ---------------------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------------------


class ControllerLike(Protocol):
    """The slice of ``aiohomekit.Controller`` the bridge uses (fakes implement this)."""

    async def async_start(self) -> None: ...

    async def async_stop(self) -> None: ...

    async def async_find(self, device_id: str, timeout: float = ...) -> Any: ...

    def async_discover(self) -> AsyncIterator[Any]: ...

    def load_pairing(self, alias: str, pairing_data: dict[str, Any]) -> Any: ...

    async def remove_pairing(self, alias: str) -> None: ...


ControllerFactory = Callable[[Path], ControllerLike]
EventHook = Callable[[str, set[int]], None]
SaveFn = Callable[[dict[str, Any]], "Awaitable[None] | None"]


@dataclass(frozen=True)
class DiscoveredDevice:
    id: str  # lower-case 'aa:bb:cc:dd:ee:ff'
    name: str
    model: str | None
    category: int | None
    address: str | None
    port: int | None
    status_flags: int
    config_num: int | None

    @property
    def unpaired(self) -> bool:
        return bool(self.status_flags & STATUS_UNPAIRED)


@dataclass
class ReadResult:
    alias: str
    ok: bool
    values: dict[int, dict[str, Any]] = field(default_factory=dict)  # aid -> {key: value}
    missing: int = 0  # entries that came back with a status instead of a value
    available: bool = True
    needs_repair: bool = False
    error: str | None = None


@dataclass
class _Pending:
    alias: str
    device_id: str
    discovery: Any
    finish: Callable[[str], Awaitable[Any]]
    started: float
    device: DiscoveredDevice


@dataclass
class _Paired:
    alias: str
    device_id: str
    pairing: Any
    pairing_data: dict[str, Any]  # in memory only (holds iOSDeviceLTSK); never logged
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    parsed: ParsedAccessories | None = None
    subscribed: set[tuple[int, int]] = field(default_factory=set)
    values: dict[int, dict[str, Any]] = field(default_factory=dict)
    failures: int = 0
    available: bool = True
    needs_repair: bool = False
    config_changed: bool = False
    unsubs: list[Callable[[], None]] = field(default_factory=list)


def _discovered(d: Any) -> DiscoveredDevice:
    desc = d.description
    cat = getattr(desc, "category", None)
    port = getattr(desc, "port", None)
    return DiscoveredDevice(
        id=str(desc.id).lower(),
        name=str(getattr(desc, "name", "") or desc.id),
        model=_clean_str(getattr(desc, "model", None)),
        category=None if cat is None else int(cat),
        address=_clean_str(getattr(desc, "address", None)),
        port=None if port is None else int(port),
        status_flags=int(getattr(desc, "status_flags", 0) or 0),
        config_num=None if getattr(desc, "config_num", None) is None else int(desc.config_num),
    )


def _status(entry: Any) -> int:
    if isinstance(entry, Mapping):
        try:
            return int(entry.get("status", 0) or 0)
        except (TypeError, ValueError):
            return -1
    return 0


class HomekitBridge:
    """One HomeKit controller for the process. Not thread-safe; use from one event loop."""

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        controller_factory: ControllerFactory | None = None,
        on_event: EventHook | None = None,
        find_timeout: float = FIND_TIMEOUT_S,
    ) -> None:
        if state_dir is None:
            from climate.config import get_settings

            state_dir = get_settings().homekit_state_dir
        self.state_dir = Path(state_dir)
        self._factory = controller_factory
        self.on_event = on_event
        self._find_timeout = find_timeout
        self._controller: Any = None
        self._azc: Any = None
        self._browser: Any = None
        self._pending: dict[str, _Pending] = {}
        self._paired: dict[str, _Paired] = {}

    # --- lifecycle --------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._controller is not None

    async def start(self) -> None:
        if self._controller is not None:
            return
        ensure_state_dir(self.state_dir)
        if self._factory is not None:
            controller = self._factory(self.state_dir)
            await controller.async_start()
            self._controller = controller
            return
        from aiohomekit import Controller
        from aiohomekit.characteristic_cache import CharacteristicCacheFile
        from aiohomekit.zeroconf import ZeroconfServiceListener
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        azc = AsyncZeroconf()
        # BOTH types, or the Controller fails to start (the CoAP transport needs _udp).
        browser = AsyncServiceBrowser(azc.zeroconf, list(HAP_TYPES), listener=ZeroconfServiceListener())
        controller = Controller(
            async_zeroconf_instance=azc, char_cache=CharacteristicCacheFile(self.state_dir / "charmap.json")
        )
        try:
            await controller.async_start()
        except BaseException:
            await browser.async_cancel()
            await azc.async_close()
            raise
        self._azc, self._browser, self._controller = azc, browser, controller
        log.info("homekit controller started (state dir %s)", self.state_dir)

    async def stop(self) -> None:
        for device_id in list(self._pending):
            await self.cancel_pairing(device_id)
        for alias in list(self._paired):
            await self.unload(alias)
        controller, self._controller = self._controller, None
        if controller is not None:
            with contextlib.suppress(Exception):
                await controller.async_stop()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.async_cancel()
            self._browser = None
        if self._azc is not None:
            with contextlib.suppress(Exception):
                await self._azc.async_close()
            self._azc = None

    def _ctl(self) -> Any:
        if self._controller is None:
            raise HomekitError("the HomeKit controller is not started")
        return self._controller

    # --- discovery --------------------------------------------------------------------

    async def discover(self) -> list[DiscoveredDevice]:
        """Everything mDNS has shown so far (IP and CoAP), one entry per device id."""
        seen: dict[str, DiscoveredDevice] = {}
        async for d in self._ctl().async_discover():
            try:
                dev = _discovered(d)
            except (AttributeError, TypeError, ValueError):
                continue
            seen.setdefault(dev.id, dev)
        return sorted(seen.values(), key=lambda x: x.id)

    async def find(self, device_id: str) -> DiscoveredDevice:
        try:
            d = await self._ctl().async_find(device_id.lower(), timeout=self._find_timeout)
        except Exception as exc:
            raise HomekitPairingError(pairing_error_message(exc)) from exc
        return _discovered(d)

    # --- pairing ----------------------------------------------------------------------

    def has_pending(self, device_id: str) -> bool:
        return device_id.lower() in self._pending

    def pending_device_ids(self) -> list[str]:
        return list(self._pending)

    def pending_age(self, device_id: str) -> float | None:
        p = self._pending.get(device_id.lower())
        return None if p is None else time.monotonic() - p.started

    async def begin_pairing(self, device_id: str, alias: str) -> DiscoveredDevice:
        """Start pair-setup (SRP M1). The thermostat now shows a fresh 8-digit code."""
        device_id = device_id.lower()
        await self.cancel_pairing(device_id)
        if alias in self._paired:
            raise HomekitPairingError(f"The alias {alias!r} is already in use by a loaded pairing.")
        try:
            discovery = await self._ctl().async_find(device_id, timeout=self._find_timeout)
            device = _discovered(discovery)
            finish = await discovery.async_start_pairing(alias)
        except Exception as exc:
            log.info("homekit pairing start failed for %s: %s", device_id, type(exc).__name__)
            raise HomekitPairingError(pairing_error_message(exc)) from exc
        self._pending[device_id] = _Pending(alias, device_id, discovery, finish, time.monotonic(), device)
        log.info("homekit pairing started for %s as %r; waiting for the code", device_id, alias)
        return device

    async def cancel_pairing(self, device_id: str) -> None:
        p = self._pending.pop(device_id.lower(), None)
        if p is not None:
            await self._close_discovery(p)

    async def finish_pairing(self, device_id: str, code: str, *, save: SaveFn) -> dict[str, Any]:
        """Finish pair-setup with the code shown on the thermostat.

        ``save(pairing_data)`` is called FIRST (it must persist the data, encrypted); only then
        is the temporary pairing closed. If saving fails the new pairing is removed from the
        thermostat again so it is not left paired to lost keys. Returns the pairing data."""
        device_id = device_id.lower()
        code = code.strip()
        if not CODE_RE.fullmatch(code):
            raise HomekitPairingError(BAD_CODE_FORMAT_MSG)  # the pending pairing stays usable
        pending = self._pending.pop(device_id, None)
        if pending is None:
            raise HomekitPairingError(NO_PENDING_MSG)
        try:
            pairing = await pending.finish(code)
        except Exception as exc:
            await self._close_discovery(pending)
            log.info("homekit pairing finish failed for %s: %s", device_id, type(exc).__name__)
            raise HomekitPairingError(pairing_error_message(exc)) from exc
        data = dict(pairing.pairing_data)
        try:
            result = save(data)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            log.warning("homekit: saving the new pairing for %s failed (%s)", device_id, type(exc).__name__)
            removed = await self._rollback(pairing, data)
            await self._close_temporary(pairing, pending.alias)
            hint = (
                "it was removed from the thermostat again; try pairing again."
                if removed
                else "the thermostat may still hold it: choose Disconnect from HomeKit on the thermostat, then pair again."
            )
            raise HomekitPairingError(f"The pairing could not be saved ({type(exc).__name__}); {hint}") from exc
        await self._close_temporary(pairing, pending.alias)
        log.info("homekit pairing saved for %s as %r", device_id, pending.alias)
        return data

    async def _rollback(self, pairing: Any, data: Mapping[str, Any]) -> bool:
        try:
            await pairing.remove_pairing(str(data["iOSPairingId"]))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("homekit: could not remove the unsaved pairing: %s", type(exc).__name__)
            return False

    async def _close_temporary(self, pairing: Any, alias: str) -> None:
        with contextlib.suppress(Exception):
            await pairing.close()
        # finish_pairing registers the object on the IP transport under the alias; drop it so
        # only the pairing loaded with load_pairing (keyed by device id) stays.
        sub = getattr(pairing, "controller", None)
        pairings = getattr(sub, "pairings", None)
        if isinstance(pairings, dict) and pairings.get(alias) is pairing:
            pairings.pop(alias, None)

    @staticmethod
    async def _close_discovery(p: _Pending) -> None:
        close = getattr(p.discovery, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()

    # --- loaded pairings --------------------------------------------------------------

    def aliases(self) -> list[str]:
        return list(self._paired)

    def is_loaded(self, alias: str) -> bool:
        return alias in self._paired

    def device_id(self, alias: str) -> str:
        return self._entry(alias).device_id

    def is_available(self, alias: str) -> bool:
        ent = self._paired.get(alias)
        return ent is not None and ent.available and not ent.needs_repair

    def needs_repair(self, alias: str) -> bool:
        ent = self._paired.get(alias)
        return ent is not None and ent.needs_repair

    def values(self, alias: str) -> dict[int, dict[str, Any]]:
        """Latest known values (polls + pushed events), aid -> {key: value}."""
        return {aid: dict(v) for aid, v in self._entry(alias).values.items()}

    def inventory_of(self, alias: str) -> list[dict[str, Any]] | None:
        ent = self._entry(alias)
        return None if ent.parsed is None else ent.parsed.inventory

    def _entry(self, alias: str) -> _Paired:
        ent = self._paired.get(alias)
        if ent is None:
            raise HomekitError(f"no HomeKit pairing loaded for {alias!r}")
        return ent

    async def load(self, alias: str, pairing_data: Mapping[str, Any]) -> None:
        """Register a saved pairing with the running controller (keyed by device id so mDNS
        address updates reach it)."""
        data = dict(pairing_data)
        if alias in self._paired:
            await self.unload(alias)
        pairing = self._ctl().load_pairing(alias, dict(data))
        ent = _Paired(alias=alias, device_id=str(data.get("AccessoryPairingID", "")).lower(), pairing=pairing,
                      pairing_data=data)
        self._paired[alias] = ent
        self._attach(ent)
        log.info("homekit pairing %r loaded (%s)", alias, ent.device_id)

    def _attach(self, ent: _Paired) -> None:
        pairing = ent.pairing
        ent.unsubs = [pairing.dispatcher_connect(lambda ev, _p=pairing, _a=ent.alias: self._on_dispatch(_a, _p, ev))]
        cfg = getattr(pairing, "dispatcher_connect_config_changed", None)
        if cfg is not None:
            ent.unsubs.append(cfg(lambda _n, _p=pairing, _a=ent.alias: self._on_config_changed(_a, _p)))

    @staticmethod
    def _detach(ent: _Paired) -> None:
        for unsub in ent.unsubs:
            with contextlib.suppress(Exception):
                unsub()
        ent.unsubs = []

    async def _shutdown_object(self, ent: _Paired) -> None:
        self._detach(ent)
        pairing = ent.pairing
        stop = getattr(pairing, "shutdown", None) or getattr(pairing, "close", None)
        if stop is not None:
            with contextlib.suppress(Exception):
                await stop()
        for ctl in (self._controller, getattr(pairing, "controller", None)):
            if ctl is None:
                continue
            aliases = getattr(ctl, "aliases", None)
            if isinstance(aliases, dict) and aliases.get(ent.alias) is pairing:
                aliases.pop(ent.alias, None)
            pairings = getattr(ctl, "pairings", None)
            if isinstance(pairings, dict) and pairings.get(ent.device_id) is pairing:
                pairings.pop(ent.device_id, None)

    async def unload(self, alias: str) -> None:
        """Forget a pairing locally (closes the connection; the device stays paired)."""
        ent = self._paired.pop(alias, None)
        if ent is not None:
            await self._shutdown_object(ent)

    def needs_recreate(self, alias: str) -> bool:
        """True once aiohomekit permanently disabled push on this pairing object."""
        ent = self._paired.get(alias)
        return ent is not None and getattr(ent.pairing, "supports_subscribe", True) is False

    async def recreate(self, alias: str) -> None:
        """Replace the pairing object (push stays off forever on the old one); keep state."""
        ent = self._entry(alias)
        async with ent.lock:
            await self._shutdown_object(ent)
            ent.pairing = self._ctl().load_pairing(alias, dict(ent.pairing_data))
            ent.subscribed = set()
            self._attach(ent)
        log.info("homekit pairing %r recreated to restore push events", alias)

    async def remove(self, alias: str) -> Literal["removed", "already_removed"]:
        """Unpair: remove our pairing ON THE DEVICE (it must be reachable) and forget it.

        Returns 'already_removed' when the device no longer accepts our keys. Raises
        HomekitUnavailable when it cannot be reached; the saved keys are then still valid."""
        ent = self._paired.pop(alias, None)
        if ent is None:
            raise HomekitError(f"no HomeKit pairing loaded for {alias!r}")
        self._detach(ent)
        try:
            await self._ctl().remove_pairing(alias)
        except Exception as exc:
            await self._shutdown_object(ent)
            if _is(exc, "AuthenticationError", "UnpairedError"):
                log.info("homekit %r: the device had already forgotten this pairing", alias)
                return "already_removed"
            log.info("homekit %r: unpair failed: %s", alias, type(exc).__name__)
            raise HomekitUnavailable(
                f"Could not reach the thermostat to unpair ({_short(exc)}). It must be reachable; otherwise "
                "choose Disconnect from HomeKit on the thermostat, then unpair again."
            ) from exc
        await self._shutdown_object(ent)
        log.info("homekit %r unpaired", alias)
        return "removed"

    # --- events -----------------------------------------------------------------------

    def _on_dispatch(self, alias: str, pairing: Any, event: Mapping[tuple[int, int], Any]) -> None:
        """aiohomekit listener (sync, on the event loop). Receives {} on every (re)connect and
        optimistic echoes of our own writes; only subscribed read characteristics count."""
        try:
            ent = self._paired.get(alias)
            if ent is None or ent.pairing is not pairing or ent.parsed is None:
                return
            if not event:
                self._emit(alias, set())
                return
            changed: set[int] = set()
            for key, payload in event.items():
                if key not in ent.subscribed or not isinstance(payload, Mapping) or "value" not in payload:
                    continue
                name = ent.parsed.index.get(key)
                if name is None:
                    continue
                ent.values.setdefault(key[0], {})[name] = payload["value"]
                changed.add(key[0])
            if changed:
                self._emit(alias, changed)
        except Exception:  # never raise into the library
            log.exception("homekit %r: event handling failed", alias)

    def _emit(self, alias: str, aids: set[int]) -> None:
        if self.on_event is not None:
            self.on_event(alias, aids)

    def _on_config_changed(self, alias: str, pairing: Any) -> None:
        ent = self._paired.get(alias)
        if ent is not None and ent.pairing is pairing:
            ent.config_changed = True

    def take_config_changed(self, alias: str) -> bool:
        ent = self._paired.get(alias)
        if ent is None or not ent.config_changed:
            return False
        ent.config_changed = False
        return True

    # --- failures ---------------------------------------------------------------------

    def _failure(self, ent: _Paired, exc: BaseException) -> HomekitError:
        """Count a failed request. Unavailable after 3 in a row, or at once on
        AccessoryNotFoundError; AuthenticationError means the pairing is gone on the device."""
        ent.failures += 1
        if _is(exc, "AuthenticationError"):
            ent.needs_repair = True
            ent.available = False
            return HomekitAuthError(REPAIR_MSG)
        if _is(exc, "AccessoryNotFoundError") or ent.failures >= FAILS_UNTIL_UNAVAILABLE:
            ent.available = False
        return HomekitUnavailable(f"HomeKit request failed ({_short(exc)})")

    def _success(self, ent: _Paired) -> None:
        ent.failures = 0
        ent.available = True
        ent.needs_repair = False

    # --- inventory / subscribe / read -------------------------------------------------

    async def inventory(self, alias: str) -> list[dict[str, Any]]:
        """Fetch /accessories and index the characteristics we use. Values in it may be stale
        (ecobee after a reboot; charmap cache), so they are NOT used as readings."""
        ent = self._entry(alias)
        async with ent.lock:
            return await self._inventory_locked(ent)

    async def _inventory_locked(self, ent: _Paired) -> list[dict[str, Any]]:
        try:
            raw = await ent.pairing.list_accessories_and_characteristics()
        except Exception as exc:
            raise self._failure(ent, exc) from exc
        ent.parsed = parse_accessories(raw)
        ent.values = {}
        ent.config_changed = False
        self._success(ent)
        return ent.parsed.inventory

    async def subscribe(self, alias: str) -> int:
        """Subscribe the standard sensor characteristics that carry 'ev'. Returns how many are
        active. Polling stays the source of truth either way."""
        ent = self._entry(alias)
        async with ent.lock:
            if ent.parsed is None:
                await self._inventory_locked(ent)
            assert ent.parsed is not None
            chars = sorted(
                k for k, name in ent.parsed.index.items() if name in PUSH_KEYS and "ev" in ent.parsed.perms.get(k, [])
            )
            if not chars:
                ent.subscribed = set()
                return 0
            if getattr(ent.pairing, "supports_subscribe", True) is False:
                return 0
            try:
                res = await ent.pairing.subscribe(chars)
            except Exception as exc:  # noqa: BLE001
                log.info("homekit %r: subscribe failed: %s", ent.alias, type(exc).__name__)
                return 0
            if getattr(ent.pairing, "supports_subscribe", True) is False:
                ent.subscribed = set()
                return 0
            bad = {k for k, v in (res or {}).items() if _status(v) != 0}
            ent.subscribed = set(chars) - bad
            return len(ent.subscribed)

    async def read(self, alias: str) -> ReadResult:
        """Poll every readable characteristic we use, in batches of <= 49, one request at a
        time. Entries carrying 'status' instead of 'value' are missing data."""
        ent = self._entry(alias)
        async with ent.lock:
            try:
                if ent.parsed is None:
                    await self._inventory_locked(ent)
                assert ent.parsed is not None
                keys = sorted(
                    k
                    for k, name in ent.parsed.index.items()
                    if name not in WRITE_ONLY_KEYS and "pr" in ent.parsed.perms.get(k, [])
                )
                values: dict[int, dict[str, Any]] = {}
                missing = 0
                for i in range(0, len(keys), MAX_BATCH):
                    batch = keys[i : i + MAX_BATCH]
                    try:
                        res = await ent.pairing.get_characteristics(batch)
                    except Exception as exc:
                        raise self._failure(ent, exc) from exc
                    for k in batch:
                        entry = (res or {}).get(k)
                        if not isinstance(entry, Mapping) or "value" not in entry or _status(entry) != 0:
                            missing += 1
                            continue
                        values.setdefault(k[0], {})[ent.parsed.index[k]] = entry["value"]
            except HomekitError as exc:
                return ReadResult(alias, ok=False, available=ent.available, needs_repair=ent.needs_repair,
                                  error=str(exc))
            self._success(ent)
            for aid in {k[0] for k in keys}:
                ent.values[aid] = dict(values.get(aid, {}))
            return ReadResult(alias, ok=True, values=values, missing=missing)

    # --- writes -----------------------------------------------------------------------

    def _iid(self, ent: _Paired, key: str, aid: int = THERMOSTAT_AID) -> tuple[int, int]:
        assert ent.parsed is not None
        for (a, iid), name in ent.parsed.index.items():
            if a == aid and name == key:
                return (a, iid)
        raise HomekitWriteError(f"this thermostat does not expose {key}")

    async def _put(self, ent: _Paired, rows: list[tuple[int, int, Any]]) -> None:
        """put_characteristics with the write guard; any entry with status != 0 fails."""
        assert ent.parsed is not None
        for aid, iid, _ in rows:
            ctype = ent.parsed.types.get((aid, iid))
            if ctype is None or ctype in FORBIDDEN_WRITE_TYPES or KEY_BY_TYPE.get(ctype) not in ALLOWED_WRITE_KEYS:
                raise HomekitWriteForbidden(f"refusing to write characteristic {aid}.{iid} ({ctype})")
        try:
            res = await ent.pairing.put_characteristics(rows)
        except Exception as exc:
            raise HomekitWriteError(f"write failed ({_short(exc)})") from exc
        failed = {k: v for k, v in (res or {}).items() if _status(v) != 0}
        if failed:
            detail = ", ".join(
                f"{k[0]}.{k[1]}: {v.get('status')} {v.get('description', '')}".strip() for k, v in failed.items()
            )
            raise HomekitWriteError(f"the thermostat rejected the write ({detail})")

    async def _get(self, ent: _Paired, keys: list[tuple[int, int]]) -> dict[tuple[int, int], Any]:
        try:
            res = await ent.pairing.get_characteristics(keys)
        except Exception as exc:
            raise HomekitWriteError(f"read failed ({_short(exc)})") from exc
        out: dict[tuple[int, int], Any] = {}
        for k in keys:
            entry = (res or {}).get(k)
            if isinstance(entry, Mapping) and "value" in entry and _status(entry) == 0:
                out[k] = entry["value"]
        return out

    async def set_climate_hold(self, alias: str, climate: ClimateName, until: datetime, tz: str) -> WriteResult:
        """Timed climate hold: GET TIMESTAMP (learn the suffix), PUT TIMESTAMP alone, then a
        SEPARATE PUT of SET_HOLD_SCHEDULE, then read back TIMESTAMP and CURRENT_MODE."""
        if climate not in CLIMATE_CODES:
            raise ValueError(f"climate must be one of {sorted(CLIMATE_CODES)}")
        if until.tzinfo is None:
            raise ValueError("until must be timezone-aware")
        ZoneInfo(tz)  # an unknown zone raises here, before anything is written
        code = CLIMATE_CODES[climate]
        ent = self._entry(alias)
        request: dict[str, object] = {"kind": "climate_hold", "climate": climate, "until": until.isoformat()}
        before: dict[str, object] = {}
        async with ent.lock:
            try:
                if ent.parsed is None:
                    await self._inventory_locked(ent)
                ts_k = self._iid(ent, "VENDOR_ECOBEE_TIMESTAMP")
                hold_k = self._iid(ent, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE")
                mode_k = self._iid(ent, "VENDOR_ECOBEE_CURRENT_MODE")
                cur = await self._get(ent, [ts_k, mode_k])
                before = {"timestamp": cur.get(ts_k), "current_mode": cur.get(mode_k)}
                cur_ts = cur.get(ts_k)
                if not isinstance(cur_ts, str):
                    raise HomekitWriteError("could not read VENDOR_ECOBEE_TIMESTAMP to learn this unit's format")
                value = format_hold_end(until, tz, timestamp_suffix(cur_ts))
                request["timestamp"] = value
                request["set_hold_schedule"] = code
                await self._put(ent, [(ts_k[0], ts_k[1], value)])  # end time first, alone
                await self._put(ent, [(hold_k[0], hold_k[1], code)])  # then the hold, separately
                back = await self._get(ent, [ts_k, mode_k])
            except HomekitWriteForbidden:
                raise
            except HomekitError as exc:
                return WriteResult(ok=False, channel="homekit", before=before, request=request, error=str(exc))
        readback: dict[str, object] = {
            "timestamp": back.get(ts_k),
            "current_mode": back.get(mode_k),
            "expected_timestamp": value,
            "expected_mode": code,
        }
        problems: list[str] = []
        rb_ts = back.get(ts_k)
        try:
            end = parse_timestamp(rb_ts, tz) if isinstance(rb_ts, str) else None
        except (HomekitError, ValueError):
            end = None
        if end is None or abs((end - until).total_seconds()) > 60:
            problems.append(f"hold end reads {rb_ts!r}, expected {value!r}")
        if back.get(mode_k) != code:
            problems.append(f"current mode reads {back.get(mode_k)!r}, expected {code} ({climate})")
        return WriteResult(ok=not problems, channel="homekit", before=before, request=request, readback=readback,
                           error="; ".join(problems) or None)

    async def clear_hold(self, alias: str) -> WriteResult:
        """Resume the schedule: PUT CLEAR_HOLD true, then read back TIMESTAMP and CURRENT_MODE.
        ok means the thermostat accepted the command and the read-back succeeded."""
        ent = self._entry(alias)
        request: dict[str, object] = {"kind": "clear_hold"}
        before: dict[str, object] = {}
        async with ent.lock:
            try:
                if ent.parsed is None:
                    await self._inventory_locked(ent)
                clear_k = self._iid(ent, "VENDOR_ECOBEE_CLEAR_HOLD")
                ts_k = self._iid(ent, "VENDOR_ECOBEE_TIMESTAMP")
                mode_k = self._iid(ent, "VENDOR_ECOBEE_CURRENT_MODE")
                cur = await self._get(ent, [ts_k, mode_k])
                before = {"timestamp": cur.get(ts_k), "current_mode": cur.get(mode_k)}
                await self._put(ent, [(clear_k[0], clear_k[1], True)])
                back = await self._get(ent, [ts_k, mode_k])
            except HomekitWriteForbidden:
                raise
            except HomekitError as exc:
                return WriteResult(ok=False, channel="homekit", before=before, request=request, error=str(exc))
        readback: dict[str, object] = {"timestamp": back.get(ts_k), "current_mode": back.get(mode_k)}
        ok = ts_k in back and mode_k in back
        return WriteResult(ok=ok, channel="homekit", before=before, request=request, readback=readback,
                           error=None if ok else "read-back after clearing the hold returned no values")

    async def read_comfort_targets(self, alias: str) -> dict[str, dict[str, float | None]]:
        """READ the Home / Sleep / Away target setpoints: {climate: {'heat_f', 'cool_f'}} in °F
        (None when the thermostat does not expose one or returns no value). One GET, never a
        put: these characteristics are FORBIDDEN_WRITE_TYPES. Raises HomekitError on failure."""
        ent = self._entry(alias)
        async with ent.lock:
            if ent.parsed is None:
                await self._inventory_locked(ent)
            assert ent.parsed is not None
            where: dict[tuple[str, str], tuple[int, int]] = {}
            for climate, (heat_t, cool_t) in COMFORT_TARGET_TYPES.items():
                for side, ctype in (("heat_f", heat_t), ("cool_f", cool_t)):
                    k = next((k for k, t in sorted(ent.parsed.types.items())
                              if k[0] == THERMOSTAT_AID and t == ctype and "pr" in ent.parsed.perms.get(k, [])), None)
                    if k is not None:
                        where[(climate, side)] = k
            got = await self._get(ent, sorted(set(where.values()))) if where else {}
        out: dict[str, dict[str, float | None]] = {c: {"heat_f": None, "cool_f": None} for c in COMFORT_TARGET_TYPES}
        for (climate, side), k in where.items():
            out[climate][side] = setpoint_f(got.get(k))
        return out


def hold_window(now: datetime, max_hours: int) -> tuple[datetime, datetime]:
    """Accepted range for a queued hold's end: more than 5 minutes ahead, at most
    ``max_hours`` (+5 min of queueing slack) ahead. HomeKit holds are 1-2 h, renewed."""
    return now + timedelta(minutes=5), now + timedelta(hours=max_hours, minutes=5)

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
(they are only ever READ: polled, and read again just before a climate hold to check what
that hold would hold). ``snapshot_from_values`` turns a poll into a UnitSnapshot for when the
ecobee cloud is down; ``detect_hand_change`` spots a change someone made at the thermostat
that HomeKit can see (a comfort setting picked by hand, a temperature set by hand).

Addresses (DHCP): a thermostat's identity is its HAP device id, never an IP. aiohomekit's
pairing data carries the address the thermostat had when it was paired (``AccessoryIP`` /
``AccessoryIPs`` / ``AccessoryPort``, required by its pairing format); a loaded pairing connects
there only until the IP transport holds an mDNS description for its device id, and from then on
every connection attempt goes to the addresses mDNS last reported (an update also cuts the
reconnect wait short). So the bridge: waits briefly for the live mDNS record before loading a
pairing and hands aiohomekit that address (else the last address mDNS reported, the caller's
hint; the pairing-time address only as a last resort), never rewriting the saved keys; notes
every mDNS change so the service re-reads discovery at once; replaces a pairing object still
connected to an address the thermostat has left; and while a pairing keeps failing, asks the
network (mDNS) where its device id is now, with backoff, because aiohomekit itself only learns a
new address from an announcement it happens to hear.

This module is a pure adapter: no database access. aiohomekit is imported lazily so the rest
of the stack (which does not install the ``homekit`` extra) can import these helpers.
Tests inject a fake controller through ``HomekitBridge(controller_factory=...)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from climate.house import ROOM_BY_KEY, SENSORS, normalize_name
from climate.sources.base import (
    KNOWN_HOLD_TYPES,
    PROTECTED_EVENTS,
    HoldInfo,
    HvacMode,
    SensorReading,
    UnitSnapshot,
    WriteResult,
)

log = logging.getLogger("climate.homekit")

HAP_TYPES = ("_hap._tcp.local.", "_hap._udp.local.")
CODE_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")
MAX_BATCH = 49  # characteristics per GET (Home Assistant's limit)
FAILS_UNTIL_UNAVAILABLE = 3
FIND_TIMEOUT_S = 30.0
THERMOSTAT_AID = 1
STATUS_UNPAIRED = 0x01

# --- addresses follow mDNS (DHCP) ---
# aiohomekit resolves a changed service 0.5 s after the browser reports it: discovery is re-read
# once mDNS has been quiet this long (or at the latest MDNS_MAX_WAIT_S after the first change).
MDNS_SETTLE_S = 1.0
MDNS_MAX_WAIT_S = 5.0
# How long load() waits for the live mDNS record of a device id before falling back to the
# last address mDNS reported. The browser queries at start; thermostats answer within a second.
LOAD_FIND_TIMEOUT_S = 5.0
# While a pairing keeps failing: ask the network where its device id is now, at once, then
# after each of these waits (the last one repeats) until a request succeeds again.
RESOLVE_BACKOFF_S: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
# DNS record types / class used in those queries (RFC 1035, RFC 2782, RFC 3596).
_DNS_CLASS_IN, _DNS_A, _DNS_PTR, _DNS_TXT, _DNS_AAAA, _DNS_SRV = 1, 1, 12, 16, 28, 33

ClimateName = Literal["home", "sleep", "away"]
CLIMATE_CODES: dict[str, int] = {"home": 0, "sleep": 1, "away": 2}  # SET_HOLD_SCHEDULE values
# VENDOR_ECOBEE_CURRENT_MODE readings: home 0, sleep 1, away 2, temp 3.
MODE_CLIMATES: dict[int, str] = {code: name for name, code in CLIMATE_CODES.items()}
MODE_TEMPERATURE_HOLD = 3
HVAC_TARGETS: dict[int, str] = {0: "off", 1: "heat", 2: "cool", 3: "auto"}  # HEATING_COOLING_TARGET
OUR_HOLD_MATCH_S = 120  # TIMESTAMP within this of our hold's end = our hold is the one running
# UnitSnapshot.settings key of every HomeKit snapshot: when HomeKit took over the unit's live
# snapshot (ISO UTC). climate.state ignores a HomeKit hand-change row older than it: such a row
# belongs to an earlier outage, and the cloud has reported the real hold state since.
HOMEKIT_SINCE_KEY = "homekit_since"

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
    # Each comfort setting's own targets, °C. Polled READ ONLY (they tell a temperature set by
    # hand from the comfort setting in force); FORBIDDEN_WRITE_TYPES keeps them unwritable.
    "VENDOR_ECOBEE_HOME_TARGET_HEAT": "E4489BBC-5227-4569-93E5-B345E3E5508F",
    "VENDOR_ECOBEE_HOME_TARGET_COOL": "7D381BAA-20F9-40E5-9BE9-AEB92D4BECEF",
    "VENDOR_ECOBEE_SLEEP_TARGET_HEAT": "05B97374-6DC0-439B-A0FA-CA33F612D425",
    "VENDOR_ECOBEE_SLEEP_TARGET_COOL": "A251F6E7-AC46-4190-9C5D-3D06277BDF9F",
    "VENDOR_ECOBEE_AWAY_TARGET_HEAT": "73AAB542-892A-4439-879A-D2A883724B69",
    "VENDOR_ECOBEE_AWAY_TARGET_COOL": "5DA985F0-898A-4850-B987-B76C6C78D670",
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
# that hold would hold (a climate hold holds whatever the schedule's setpoints are), and
# polled (by their CHAR_TYPES keys) to tell a temperature set by hand from the comfort setting.
COMFORT_TARGET_TYPES: dict[str, tuple[str, str]] = {
    "home": ("E4489BBC-5227-4569-93E5-B345E3E5508F", "7D381BAA-20F9-40E5-9BE9-AEB92D4BECEF"),
    "sleep": ("05B97374-6DC0-439B-A0FA-CA33F612D425", "A251F6E7-AC46-4190-9C5D-3D06277BDF9F"),
    "away": ("73AAB542-892A-4439-879A-D2A883724B69", "5DA985F0-898A-4850-B987-B76C6C78D670"),
}
COMFORT_TARGET_KEYS: dict[str, tuple[str, str]] = {
    climate: (KEY_BY_TYPE[heat_t], KEY_BY_TYPE[cool_t]) for climate, (heat_t, cool_t) in COMFORT_TARGET_TYPES.items()
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


class HomekitWriteRejected(HomekitWriteError):
    """The thermostat answered a put with a non-zero status: that write did not land (unlike a
    put whose answer was lost, which may have)."""


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
    """Does the thermostat still show ``prev`` (a hold from the last live snapshot)?

    HomeKit cannot see ecobee events, so a vacation or utility event with a known end is kept
    until that end whatever the mode shows, and so is an unrecognised ecobee event (outside
    ``KNOWN_HOLD_TYPES``: its setpoints may look like a comfort setting, or be relative), also
    without an end (then until the cloud is back): the controller stays hands-off while it
    runs, and its setpoints are never read as a person's change."""
    if prev.end is not None and prev.end <= ts:
        return False
    if prev.hold_type in PROTECTED_EVENTS and prev.end is not None:
        return True  # scheduled events with a known end: kept until that end
    if prev.hold_type is not None and prev.hold_type not in KNOWN_HOLD_TYPES:
        return True  # an unrecognised ecobee event: kept until its end, or until the cloud is back
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
    continuing: bool | None = None,
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

    ``settings['homekit_since']`` (``HOMEKIT_SINCE_KEY``) records when HomeKit took over the
    unit's live snapshot, ISO UTC: carried from ``previous`` when this snapshot replaces
    HomeKit's own (``continuing``; default: ``previous.source == 'homekit'``), else ``ts`` (this
    snapshot starts a HomeKit run). A replaced HomeKit snapshot without it (unreadable, or
    written by an older release) leaves it out: when that run started is unknown.

    Hold, first rule that applies (TIMESTAMP is ecobee's "next scheduled change": a hold's end,
    or the next schedule transition when no hold runs):
    1. ``our_hold`` = (climate, until) of our last verified HomeKit climate hold: still before
       ``until``, CURRENT_MODE shows that climate and TIMESTAMP (when readable) is within 2 min
       of ``until`` -> that climate hold, ``set_by_us``.
    2. ``previous.hold`` the thermostat still shows (not ended; climate hold while CURRENT_MODE
       is that climate; temperature hold while CURRENT_MODE is 3 and the thresholds still match;
       a vacation / demand response until its end; an unrecognised ecobee event until its end,
       or for the whole HomeKit run when it has none) -> carried as it was.
    3. CURRENT_MODE 3 -> a temperature hold at the current thresholds, ending at TIMESTAMP when
       that is in the future; not ours (the cloud channel's own holds are caught by rule 2).
    4. otherwise no hold: HomeKit cannot tell a hand-set climate hold from the schedule.

    HomeKit shows no ecobee events and no utility enrollment: ``events`` is always empty and
    ``utility`` None (an event the last cloud snapshot reported running is only carried as
    ``hold``, rule 2). Whether a person changed something is ``detect_hand_change``'s job."""
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

    if continuing is None:
        continuing = previous is not None and previous.source == "homekit"
    settings = {k: v for k, v in prev_settings.items()
                if k not in ("program_heat_f", "program_cool_f", HOMEKIT_SINCE_KEY)}
    since = prev_settings.get(HOMEKIT_SINCE_KEY) if continuing else ts.astimezone(UTC).isoformat()
    if isinstance(since, str):
        settings[HOMEKIT_SINCE_KEY] = since

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
        settings=settings,
        connected=True,
        events=[],  # never carried or invented: HomeKit cannot see ecobee events
        utility=None,
    )


# ---------------------------------------------------------------------------------------
# hand changes seen over HomeKit (while the ecobee cloud is down)
# ---------------------------------------------------------------------------------------

HAND_CHANGE_F = 0.5  # active setpoints further than this from the comfort setting's targets


@dataclass(frozen=True)
class KnownHolds:
    """What the app already knows may be on the thermostat, so that HomeKit's view of it is
    not mistaken for a person's change. Built by the homekit service from ``control_actions``,
    the last live snapshot and ``utility_events``."""

    climate: str | None = None  # our verified HomeKit climate hold, still running
    maybe_climates: frozenset[str] = frozenset()  # newer HomeKit holds of ours that may have landed
    # (heat_f, cool_f) of temperature holds that are not a new change: ours, or one the cloud
    # already reported before it went down
    temps: tuple[tuple[float | None, float | None], ...] = ()
    event: bool = False  # a vacation, utility event or unrecognised ecobee event is in force


@dataclass(frozen=True)
class HandChange:
    """A change someone made at the thermostat (or in the ecobee app, Apple Home, Siri) that
    HomeKit shows. ``heat_f`` / ``cool_f`` are the setpoints the thermostat holds now."""

    kind: Literal["comfort", "temperature"]
    climate_ref: str | None  # the comfort setting picked by hand; None for a temperature
    heat_f: float | None
    cool_f: float | None
    current_mode: int

    def same_as(self, request: Mapping[str, Any] | None) -> bool:
        """Is ``request`` (a logged detection) this same change? A comfort setting by its
        name (its setpoints may move with the schedule); a temperature by its setpoints."""
        if not isinstance(request, Mapping) or (request.get("climate_ref") or None) != self.climate_ref:
            return False
        if self.kind == "comfort":
            return True
        for mine, logged in ((self.heat_f, _num(request.get("heat_f"))), (self.cool_f, _num(request.get("cool_f")))):
            if (mine is None) != (logged is None):
                return False
            if mine is not None and logged is not None and abs(mine - logged) > 0.1:
                return False
        return True


def comfort_targets_from_values(tstat: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    """The polled Home / Sleep / Away targets: {climate: {'heat_f', 'cool_f'}} in °F, None
    where the thermostat does not expose one or returned no value."""
    return {
        climate: {"heat_f": setpoint_f(tstat.get(heat_k)), "cool_f": setpoint_f(tstat.get(cool_k))}
        for climate, (heat_k, cool_k) in COMFORT_TARGET_KEYS.items()
    }


def _relevant_sides(hvac_target: int | None) -> tuple[str, ...]:
    """The setpoints the thermostat acts on in its HEATING_COOLING_TARGET mode (both when the
    mode is auto or unknown; none when off)."""
    if hvac_target == 0:
        return ()
    if hvac_target == 1:
        return ("heat_f",)
    if hvac_target == 2:
        return ("cool_f",)
    return ("heat_f", "cool_f")


def _differs(now: Mapping[str, float | None], want: Mapping[str, float | None], sides: tuple[str, ...]) -> bool | None:
    """True when a relevant setpoint is more than HAND_CHANGE_F from ``want``; None when one
    of them cannot be compared (not exposed or not read)."""
    gaps: list[float] = []
    for side in sides:
        a, b = now.get(side), want.get(side)
        if a is None or b is None:
            return None
        gaps.append(abs(a - b))
    return any(gap > HAND_CHANGE_F for gap in gaps)


def _known_temp(now: Mapping[str, float | None], temps: Iterable[tuple[float | None, float | None]]) -> bool:
    """Does the thermostat hold the setpoints of a temperature hold we already know about?"""
    for heat, cool in temps:
        pairs = [(now.get("heat_f"), heat), (now.get("cool_f"), cool)]
        compared = [(a, b) for a, b in pairs if a is not None and b is not None]
        if compared and all(abs(a - b) <= HAND_CHANGE_F for a, b in compared):
            return True
    return False


def detect_hand_change(values: Mapping[int, Mapping[str, Any]], known: KnownHolds) -> HandChange | None:
    """A change a person made that HomeKit can see, from one poll of the thermostat (aid 1).

    HomeKit shows the comfort setting in force (CURRENT_MODE: home 0, sleep 1, away 2,
    temperature hold 3), the active heat/cool setpoints and each comfort setting's own
    targets, but no ecobee holds or events. A change is:
    (a) our HomeKit climate hold is still running (``known.climate``) but CURRENT_MODE shows
        another comfort setting (picked by hand, or the schedule after someone pressed Resume)
        or a temperature hold; or
    (b) a comfort setting shows, but the setpoints the thermostat acts on differ by more than
        0.5°F from that setting's own targets: someone set a temperature; or
    (c) a temperature hold shows (CURRENT_MODE 3) whose setpoints match none of the comfort
        settings' targets.
    (b) and (c) need the targets to be readable and are never a temperature hold we already
    know (``known.temps``). Nothing is a change while an ecobee event is in force (its
    setpoints are not a person's), when CURRENT_MODE is unreadable or a code we don't know,
    or when the thermostat shows a climate one of our own HomeKit holds may have set. A
    comfort setting picked by hand without a hold of ours running looks exactly like the
    schedule: HomeKit cannot see it."""
    tstat = values.get(THERMOSTAT_AID, {})
    mode = _int(tstat.get("VENDOR_ECOBEE_CURRENT_MODE"))
    if mode is None or known.event or (mode not in MODE_CLIMATES and mode != MODE_TEMPERATURE_HOLD):
        return None
    now_sp = {
        "heat_f": setpoint_f(tstat.get("TEMPERATURE_HEATING_THRESHOLD")),
        "cool_f": setpoint_f(tstat.get("TEMPERATURE_COOLING_THRESHOLD")),
    }
    picked = MODE_CLIMATES.get(mode)  # None while a temperature hold shows
    targets = comfort_targets_from_values(tstat)
    sides = _relevant_sides(_int(tstat.get("HEATING_COOLING_TARGET")))
    temperature = HandChange("temperature", None, now_sp["heat_f"], now_sp["cool_f"], mode)

    if known.climate is not None and picked != known.climate and picked not in known.maybe_climates:
        if picked is None:  # (a) a temperature hold replaced ours
            return None if _known_temp(now_sp, known.temps) else temperature
        shown = targets[picked]  # (a) another comfort setting replaced ours
        return HandChange("comfort", picked,
                          now_sp["heat_f"] if now_sp["heat_f"] is not None else shown["heat_f"],
                          now_sp["cool_f"] if now_sp["cool_f"] is not None else shown["cool_f"], mode)
    if picked is not None:  # (b)
        moved = _differs(now_sp, targets[picked], sides)
    else:  # (c) a temperature hold: is it one of the comfort settings' setpoints?
        each = [_differs(now_sp, t, sides) for t in targets.values()]
        moved = None if any(d is None for d in each) else all(each)
    if not moved or _known_temp(now_sp, known.temps):
        return None
    return temperature


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
    """What mDNS last reported for one HAP device id. ``address`` is the most recently
    announced usable address (IPv4 first), ``addresses`` all of them; they say where the device
    is now and are never its identity (``id`` is)."""

    id: str  # lower-case 'aa:bb:cc:dd:ee:ff'
    name: str
    model: str | None
    category: int | None
    address: str | None
    port: int | None
    status_flags: int
    config_num: int | None
    addresses: tuple[str, ...] = ()
    service: str | None = None  # full mDNS instance name, e.g. 'Hallway._hap._tcp.local.'

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
    # where mDNS last placed the device (or the address it was loaded with until then)
    address: str | None = None
    port: int | None = None
    moved: bool = False  # still connected to an address the device has left: replace the object
    resolve_task: asyncio.Task[None] | None = None


def _discovered(d: Any) -> DiscoveredDevice:
    desc = d.description
    cat = getattr(desc, "category", None)
    port = getattr(desc, "port", None)
    address = _clean_str(getattr(desc, "address", None))
    addresses = tuple(str(a) for a in (getattr(desc, "addresses", None) or ()) if a)
    if not addresses and address:
        addresses = (address,)
    raw_name, stype = _clean_str(getattr(desc, "name", None)), _clean_str(getattr(desc, "type", None))
    return DiscoveredDevice(
        id=str(desc.id).lower(),
        name=str(raw_name or desc.id),
        model=_clean_str(getattr(desc, "model", None)),
        category=None if cat is None else int(cat),
        address=address,
        port=None if port is None else int(port),
        status_flags=int(getattr(desc, "status_flags", 0) or 0),
        config_num=None if getattr(desc, "config_num", None) is None else int(desc.config_num),
        addresses=addresses,
        service=f"{raw_name}.{stype}" if raw_name and stype else None,
    )


def endpoint_data(
    pairing_data: Mapping[str, Any], address: str | None, port: int | None, addresses: Sequence[str] = ()
) -> dict[str, Any]:
    """A copy of ``pairing_data`` whose connection target is ``address``:``port`` (all of
    ``addresses`` when given). aiohomekit's IP pairing starts at ``AccessoryIP(s)`` /
    ``AccessoryPort`` and only moves on once mDNS describes the device, so this hands it the
    freshest address known. The keys are untouched; without an address the copy keeps the
    pairing-time one."""
    data = dict(pairing_data)
    if address and port:
        hosts = [str(a) for a in addresses if a] or [address]
        if address not in hosts:
            hosts.insert(0, address)
        data["AccessoryIP"], data["AccessoryIPs"], data["AccessoryPort"] = address, hosts, int(port)
    return data


def _status(entry: Any) -> int:
    if isinstance(entry, Mapping):
        try:
            return int(entry.get("status", 0) or 0)
        except (TypeError, ValueError):
            return -1
    return 0


class HomekitBridge:
    """One HomeKit controller for the process. Not thread-safe; use from one event loop.

    ``controller_factory`` (tests) replaces aiohomekit's Controller; ``zeroconf_factory``
    (tests, only with a controller factory) returns ``(async_zeroconf, browser)`` stand-ins:
    ``async_zeroconf.zeroconf`` must offer ``async_send(out)`` and ``cache.get_by_details``,
    ``browser.service_state_changed`` must offer ``register_handler`` / ``unregister_handler``."""

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        controller_factory: ControllerFactory | None = None,
        zeroconf_factory: Callable[[], tuple[Any, Any]] | None = None,
        on_event: EventHook | None = None,
        find_timeout: float = FIND_TIMEOUT_S,
        load_find_timeout: float = LOAD_FIND_TIMEOUT_S,
        mdns_settle: float = MDNS_SETTLE_S,
        resolve_backoff: Sequence[float] = RESOLVE_BACKOFF_S,
    ) -> None:
        if state_dir is None:
            from climate.config import get_settings

            state_dir = get_settings().homekit_state_dir
        self.state_dir = Path(state_dir)
        self._factory = controller_factory
        self._zeroconf_factory = zeroconf_factory
        self.on_event = on_event
        self._find_timeout = find_timeout
        self._load_find_timeout = load_find_timeout
        self._mdns_settle = mdns_settle
        self._resolve_backoff = tuple(resolve_backoff) or RESOLVE_BACKOFF_S
        self._controller: Any = None
        self._azc: Any = None
        self._browser: Any = None
        self._watched: Any = None  # the browser signal our mDNS hook is registered on
        self._pending: dict[str, _Pending] = {}
        self._paired: dict[str, _Paired] = {}
        self._gone: set[str] = set()  # lower-case mDNS instance names the browser saw go away
        self._services: dict[str, str] = {}  # device id -> its mDNS instance name, as last seen
        self._mdns_first: float | None = None  # first / latest mDNS change not yet re-read
        self._mdns_last: float = 0.0
        self._sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep  # tests record the backoff

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
            if self._zeroconf_factory is not None:
                self._azc, self._browser = self._zeroconf_factory()
                self._watch_browser()
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
        self._watch_browser()
        log.info("homekit controller started (state dir %s)", self.state_dir)

    def _watch_browser(self) -> None:
        """Hook the HAP browser so every mDNS change (a device appearing, announcing a new
        address / port / config number, or saying goodbye) is noticed at once."""
        sig = getattr(self._browser, "service_state_changed", None)
        if sig is not None:
            sig.register_handler(self._on_mdns)
            self._watched = sig

    async def stop(self) -> None:
        for device_id in list(self._pending):
            await self.cancel_pairing(device_id)
        for alias in list(self._paired):
            await self.unload(alias)
        if self._watched is not None:
            with contextlib.suppress(Exception):
                self._watched.unregister_handler(self._on_mdns)
            self._watched = None
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
        self._gone.clear()
        self._services.clear()
        self._mdns_first = None

    def _ctl(self) -> Any:
        if self._controller is None:
            raise HomekitError("the HomeKit controller is not started")
        return self._controller

    # --- discovery --------------------------------------------------------------------

    async def discover(self) -> list[DiscoveredDevice]:
        """Every device mDNS currently shows (IP and CoAP), one entry per device id, at the
        address it last announced. aiohomekit never forgets a discovery, so a service the
        browser saw leave (goodbye, or its records expired) is left out until it is back.
        Also follows each loaded pairing's device to its current address (``_note_endpoint``)."""
        seen: dict[str, DiscoveredDevice] = {}
        async for d in self._ctl().async_discover():
            try:
                dev = _discovered(d)
            except (AttributeError, TypeError, ValueError):
                continue
            if dev.service is not None and dev.service.lower() in self._gone:
                continue
            seen.setdefault(dev.id, dev)
        for dev in seen.values():
            if dev.service is not None:
                self._services[dev.id] = dev.service
        for ent in self._paired.values():
            dev = seen.get(ent.device_id)
            if dev is not None:
                self._note_endpoint(ent, dev)
        return sorted(seen.values(), key=lambda x: x.id)

    async def find(self, device_id: str) -> DiscoveredDevice:
        try:
            d = await self._ctl().async_find(device_id.lower(), timeout=self._find_timeout)
        except Exception as exc:
            raise HomekitPairingError(pairing_error_message(exc)) from exc
        return _discovered(d)

    async def _find_live(self, device_id: str) -> DiscoveredDevice | None:
        """The live mDNS record of ``device_id`` (waiting up to ``load_find_timeout`` for the
        browser to report it), or None: not seen, or seen leaving."""
        if not device_id:
            return None
        try:
            dev = _discovered(await self._ctl().async_find(device_id, timeout=self._load_find_timeout))
        except Exception:  # noqa: BLE001 - not found (yet); the caller falls back to a hint
            return None
        if dev.service is not None:
            if dev.service.lower() in self._gone:
                return None
            self._services[dev.id] = dev.service
        return dev if dev.address and dev.port else None

    # --- mDNS: changes, endpoints, re-resolution ----------------------------------------

    def _on_mdns(self, zeroconf: Any = None, service_type: str = "", name: str = "",
                 state_change: Any = None, **_: Any) -> None:
        """Browser hook (sync, on the event loop, for every HAP service that appears, changes
        or goes away): only notes it. The homekit service re-reads discovery once mDNS has
        settled (``discovery_due``); aiohomekit itself updates its description of the device
        and redirects that device's pairing to the new address."""
        try:
            if service_type not in HAP_TYPES:
                return
            key = str(name).lower()
            if getattr(state_change, "name", str(state_change)) == "Removed":
                self._gone.add(key)
            else:
                self._gone.discard(key)
            now = time.monotonic()
            self._mdns_last = now
            if self._mdns_first is None:
                self._mdns_first = now
        except Exception:  # never raise into zeroconf
            log.exception("homekit: mDNS change handling failed")

    def discovery_due(self) -> bool:
        """True once after mDNS reported a change and then stayed quiet for ``mdns_settle``
        seconds (at the latest ``MDNS_MAX_WAIT_S`` after the first change): time to re-read
        discovery so the stored address / port / config number follow the announcement."""
        if self._mdns_first is None:
            return False
        now = time.monotonic()
        if now - self._mdns_last < self._mdns_settle and now - self._mdns_first < MDNS_MAX_WAIT_S:
            return False
        self._mdns_first = None
        return True

    def _note_endpoint(self, ent: _Paired, dev: DiscoveredDevice) -> None:
        """Follow ``ent``'s device to where mDNS now shows it. aiohomekit redirects a pairing
        that is not connected by itself; one still connected to the old address (the thermostat
        moved without closing the connection) would only notice when a request times out, so
        it is marked ``moved`` and the service replaces the object (``recreate``)."""
        if not dev.address or not dev.port or (ent.address, ent.port) == (dev.address, dev.port):
            return
        was = f"{ent.address}:{ent.port}" if ent.address else "unknown"
        ent.address, ent.port = dev.address, dev.port
        host = self._connected_host(ent)
        if host is not None and host != dev.address:
            ent.moved = True
        log.info("homekit %r: %s is now at %s:%s (was %s)%s", ent.alias, ent.device_id, dev.address, dev.port, was,
                 "; reconnecting there" if ent.moved else "")

    @staticmethod
    def _connected_host(ent: _Paired) -> str | None:
        """The address the pairing's live connection talks to (None when not connected)."""
        pairing = ent.pairing
        if not getattr(pairing, "is_connected", False):
            return None
        host = getattr(getattr(pairing, "connection", None), "connected_host", None)
        return str(host) if host else None

    def endpoint(self, alias: str) -> tuple[str | None, int | None]:
        """(address, port) the pairing's device was last seen at (or loaded with)."""
        ent = self._entry(alias)
        return ent.address, ent.port

    def is_moved(self, alias: str) -> bool:
        ent = self._paired.get(alias)
        return ent is not None and ent.moved

    def take_moved(self, alias: str) -> bool:
        """True once when the pairing must be replaced to follow its device (see ``moved``)."""
        ent = self._paired.get(alias)
        if ent is None or not ent.moved:
            return False
        ent.moved = False
        return True

    def query_device(self, device_id: str) -> bool:
        """Ask the network (one mDNS query) where ``device_id`` is now: its instance's SRV and
        TXT records and its host's A / AAAA records when the instance is known, else every
        HAP service (PTR). No known answers are sent, so the device answers even when the
        cache still holds its old address; the answer reaches aiohomekit through the browser
        like any announcement. Returns False when there is no zeroconf to send with."""
        zc = getattr(self._azc, "zeroconf", None)
        if zc is None:
            return False
        try:
            from zeroconf import DNSOutgoing, DNSQuestion

            out = DNSOutgoing(0)  # flags 0: a standard query
            service = self._services.get(device_id.lower())
            if service is None:
                for hap_type in HAP_TYPES:
                    out.add_question(DNSQuestion(hap_type, _DNS_PTR, _DNS_CLASS_IN))
            else:
                out.add_question(DNSQuestion(service, _DNS_SRV, _DNS_CLASS_IN))
                out.add_question(DNSQuestion(service, _DNS_TXT, _DNS_CLASS_IN))
                srv = zc.cache.get_by_details(service, _DNS_SRV, _DNS_CLASS_IN)
                server = getattr(srv, "server", None)
                if server:
                    out.add_question(DNSQuestion(server, _DNS_A, _DNS_CLASS_IN))
                    out.add_question(DNSQuestion(server, _DNS_AAAA, _DNS_CLASS_IN))
            zc.async_send(out)
        except Exception as exc:  # noqa: BLE001 - best effort; the library keeps retrying
            log.debug("homekit: mDNS query for %s failed: %s", device_id, _short(exc))
            return False
        return True

    def _start_resolving(self, ent: _Paired) -> None:
        if ent.resolve_task is not None and not ent.resolve_task.done():
            return
        with contextlib.suppress(RuntimeError):  # no running loop: nothing to schedule on
            ent.resolve_task = asyncio.get_running_loop().create_task(self._resolve_loop(ent))

    @staticmethod
    def _stop_resolving(ent: _Paired) -> None:
        task, ent.resolve_task = ent.resolve_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _resolve_loop(self, ent: _Paired) -> None:
        """While ``ent`` keeps failing: query mDNS for its device id at once, then after each
        ``resolve_backoff`` wait (the last repeats). Ended by the next successful request, an
        unload, or a pairing the device no longer accepts (re-resolving cannot fix that)."""
        waits = itertools.chain(self._resolve_backoff, itertools.repeat(self._resolve_backoff[-1]))
        for n, wait in enumerate(waits):
            if self._paired.get(ent.alias) is not ent or ent.needs_repair or self._controller is None:
                return
            if self.query_device(ent.device_id) and n == 0:
                log.info("homekit %r: %s did not answer; asking the network (mDNS) where it is now",
                         ent.alias, ent.device_id)
            await self._sleep(wait)

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

    async def load(
        self, alias: str, pairing_data: Mapping[str, Any], *, address: str | None = None, port: int | None = None
    ) -> None:
        """Register a saved pairing with the running controller, keyed by its device id so
        every mDNS update for that id redirects it. The device is first looked up by id in the
        live mDNS browser (up to ``load_find_timeout``); aiohomekit is handed that address, else
        ``address``:``port`` (the caller's last mDNS sighting), else the pairing-time address
        saved with the keys. The saved data itself is never changed."""
        data = dict(pairing_data)
        device_id = str(data.get("AccessoryPairingID", "")).lower()
        if alias in self._paired:
            await self.unload(alias)
        live = await self._find_live(device_id)
        addresses: tuple[str, ...] = ()
        if live is not None:
            address, port, addresses = live.address, live.port, live.addresses
        elif not (address and port):
            address, port = _clean_str(data.get("AccessoryIP")), _int(data.get("AccessoryPort"))
        if alias in self._paired:  # loaded by someone else while we waited
            await self.unload(alias)
        pairing = self._ctl().load_pairing(alias, endpoint_data(data, address, port, addresses))
        ent = _Paired(alias=alias, device_id=device_id, pairing=pairing, pairing_data=data, address=address, port=port)
        self._paired[alias] = ent
        self._attach(ent)
        log.info("homekit pairing %r loaded (%s, %s at %s:%s)", alias, ent.device_id,
                 "seen on mDNS" if live is not None else "not seen on mDNS yet", address, port)

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

    async def _shutdown_object(self, ent: _Paired, *, keep_resolving: bool = False) -> None:
        self._detach(ent)
        if not keep_resolving:
            self._stop_resolving(ent)
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

    async def recreate(self, alias: str, reason: str = "to restore push events") -> None:
        """Replace the pairing object (push stays off forever on an object that lost a
        subscribe; an object still connected to an address its device has left only notices on
        a request timeout); keep state. The new object starts at the device's current address."""
        ent = self._entry(alias)
        async with ent.lock:
            await self._shutdown_object(ent, keep_resolving=True)
            ent.pairing = self._ctl().load_pairing(alias, endpoint_data(ent.pairing_data, ent.address, ent.port))
            ent.subscribed = set()
            ent.moved = False
            self._attach(ent)
        log.info("homekit pairing %r recreated %s", alias, reason)

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
        """aiohomekit listener (sync, on the event loop). Receives {} on every (re)connect (also
        before the first inventory: a pairing that could not connect yet is retried at once) and
        optimistic echoes of our own writes; only subscribed read characteristics count."""
        try:
            ent = self._paired.get(alias)
            if ent is None or ent.pairing is not pairing:
                return
            if not event:
                self._emit(alias, set())
                return
            if ent.parsed is None:
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
        AccessoryNotFoundError; AuthenticationError means the pairing is gone on the device.
        Any other failure may mean the thermostat moved to a new address without us hearing
        its announcement: start asking mDNS where it is (``_resolve_loop``)."""
        ent.failures += 1
        if _is(exc, "AuthenticationError"):
            ent.needs_repair = True
            ent.available = False
            return HomekitAuthError(REPAIR_MSG)
        if _is(exc, "AccessoryNotFoundError") or ent.failures >= FAILS_UNTIL_UNAVAILABLE:
            ent.available = False
        self._start_resolving(ent)
        return HomekitUnavailable(f"HomeKit request failed ({_short(exc)})")

    def _success(self, ent: _Paired) -> None:
        ent.failures = 0
        ent.available = True
        ent.needs_repair = False
        self._stop_resolving(ent)

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
        """put_characteristics with the write guard; any entry with status != 0 fails
        (``HomekitWriteRejected``). A put that raised may still have landed: its answer is lost."""
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
            raise HomekitWriteRejected(f"the thermostat rejected the write ({detail})")

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
        SEPARATE PUT of SET_HOLD_SCHEDULE, then read back TIMESTAMP and CURRENT_MODE.

        A failure once the SET_HOLD_SCHEDULE put is on its way (its answer lost, or the
        read-back failing), unless the thermostat rejected that put, returns ok=False WITH a
        read-back object (``sent`` True): the hold may be on the thermostat, so the homekit
        service counts it as ours (maybe landed) instead of reading it as a person's change.
        A failure before that returns no read-back: nothing that changes the mode went out."""
        if climate not in CLIMATE_CODES:
            raise ValueError(f"climate must be one of {sorted(CLIMATE_CODES)}")
        if until.tzinfo is None:
            raise ValueError("until must be timezone-aware")
        ZoneInfo(tz)  # an unknown zone raises here, before anything is written
        code = CLIMATE_CODES[climate]
        ent = self._entry(alias)
        request: dict[str, object] = {"kind": "climate_hold", "climate": climate, "until": until.isoformat()}
        before: dict[str, object] = {}
        sent = False  # the SET_HOLD_SCHEDULE put went out: from here on the hold may have landed
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
                sent = True
                await self._put(ent, [(hold_k[0], hold_k[1], code)])  # then the hold, separately
                back = await self._get(ent, [ts_k, mode_k])
            except HomekitWriteForbidden:
                raise
            except HomekitError as exc:
                if not sent or isinstance(exc, HomekitWriteRejected):
                    return WriteResult(ok=False, channel="homekit", before=before, request=request, error=str(exc))
                return WriteResult(
                    ok=False, channel="homekit", before=before, request=request,
                    readback={"sent": True, "expected_timestamp": request["timestamp"], "expected_mode": code},
                    error=f"{exc}; the hold was sent and may be on the thermostat",
                )
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
        ok means the thermostat accepted the command and the read-back succeeded. As in
        ``set_climate_hold``, a failure once the put is on its way (not rejected) keeps a
        read-back object (``sent`` True): the schedule may already be back."""
        ent = self._entry(alias)
        request: dict[str, object] = {"kind": "clear_hold"}
        before: dict[str, object] = {}
        sent = False
        async with ent.lock:
            try:
                if ent.parsed is None:
                    await self._inventory_locked(ent)
                clear_k = self._iid(ent, "VENDOR_ECOBEE_CLEAR_HOLD")
                ts_k = self._iid(ent, "VENDOR_ECOBEE_TIMESTAMP")
                mode_k = self._iid(ent, "VENDOR_ECOBEE_CURRENT_MODE")
                cur = await self._get(ent, [ts_k, mode_k])
                before = {"timestamp": cur.get(ts_k), "current_mode": cur.get(mode_k)}
                sent = True
                await self._put(ent, [(clear_k[0], clear_k[1], True)])
                back = await self._get(ent, [ts_k, mode_k])
            except HomekitWriteForbidden:
                raise
            except HomekitError as exc:
                if not sent or isinstance(exc, HomekitWriteRejected):
                    return WriteResult(ok=False, channel="homekit", before=before, request=request, error=str(exc))
                return WriteResult(ok=False, channel="homekit", before=before, request=request,
                                   readback={"sent": True},
                                   error=f"{exc}; the resume was sent and may have reached the thermostat")
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

"""Pure parsing helpers for the ecobee cloud adapter (no network, no database).

Everything here turns ecobee JSON into our types (``climate.sources.base``) and back. The
adapter in ``climate.sources.ecobee`` does the I/O and calls these. Format notes (ecobee API
v1, confirmed against the library and long-running open-source clients where the docs site
was unreachable; assumptions are marked):

- Temperatures in ``/thermostat`` (runtime, events, remote sensor capabilities, weather,
  settings ranges and ``heatCoolMinDelta``) are integers in tenths of °F; ``-5002`` and the
  string ``"unknown"`` mean no value.
- ``/runtimeReport`` values are human units: °F with one decimal, seconds 0..300 per 5-minute
  slot for equipment columns, percent for humidity, 0/1 for occupancy. Blank means no data.
- Event and report dates/times are THERMOSTAT-LOCAL wall time; ``thermostatTime`` vs
  ``utcTime`` on each thermostat gives the current offset.

``HoldInfo.hold_type`` vocabulary (what overrides the program right now; ``parse_hold``; the
tuples live in ``climate.sources.base``):

- The first RUNNING event in the thermostat's list is the one in effect (ecobee orders the
  list by running state and priority), whatever its type.
- A running EVENT that is not a plain hold carries its ecobee event type verbatim:
  ``vacation``, ``autoAway`` / ``autoHome`` (Smart Home/Away), ``quickSave`` (Smart Away
  quick save), ``demandResponse`` (utility event), or a type this app does not know
  (``today``, ``sensor``, ``switchOccupancy``, one ecobee adds later): callers treat a type
  outside ``KNOWN_HOLD_TYPES`` as hands-off. Events are never "ours": ``set_by_us`` is
  always False for them, whatever their setpoints, so callers can tell a Smart Away or a
  vacation from a hand-set hold and from the controller's own hold.
- A plain ``hold`` event (someone or something set a hold) carries how it was set, inferred
  because ecobee does not echo the request's holdType: ``holdHours``, ``nextTransition``,
  ``indefinite`` or ``dateTime`` (``PLAIN_HOLD_TYPES``). Only these can be ``set_by_us``
  (they match the last hold the controller wrote, ``app_settings['ecobee_holds']``).

Event details (``HoldInfo`` for a running event, ``ThermostatEvent`` from ``parse_events``):
``name``; ``isTemperatureAbsolute`` + ``heatHoldTemp`` / ``coolHoldTemp`` (absolute setpoints,
tenths of °F); ``isTemperatureRelative`` + ``heatRelativeTemp`` / ``coolRelativeTemp`` (the
change from the scheduled comfort setting, tenths of °F); ``isOptional`` (False = a mandatory
utility event, no opt-out); ``isCoolOff`` / ``isHeatOff``; ``dutyCyclePercentage`` (0..100;
ecobee sends 255 when unused); ``linkRef`` (stable per event). ASSUMPTION (ecobee's Issue
Demand Response example sends ``coolRelativeTemp: 40, heatRelativeTemp: 40`` for a 4°F setback
both ways; the docs site was unreachable to re-check): the relative temperatures are setback
MAGNITUDES in the energy-saving direction, so they are stored signed as cooling +x°F and
heating -x°F whatever sign arrives.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from climate.house import normalize_name
from climate.sources.base import (
    AUTO_EVENTS,
    PERSON_EVENTS,
    PLAIN_HOLD_TYPES,
    PROTECTED_EVENTS,
    HoldInfo,
    HvacMode,
    RuntimeInterval,
    SensorReading,
    ThermostatEvent,
    UtilityInfo,
)

log = logging.getLogger("climate.ecobee")

UNKNOWN_INT = -5002  # ecobee's "no value" sentinel (ECOBEE_STATE_UNKNOWN)
CALIBRATING_INT = -5003
_TS_FMT = "%Y-%m-%d %H:%M:%S"

# Columns requested from /runtimeReport (order is the request order; parsing follows the
# ``columns`` string ecobee echoes back, so a reordered response still parses).
REPORT_COLUMNS: tuple[str, ...] = (
    "auxHeat1",
    "auxHeat2",
    "compCool1",
    "compCool2",
    "compHeat1",
    "compHeat2",
    "fan",
    "hvacMode",
    "zoneAveTemp",
    "zoneHumidity",
    "zoneHeatTemp",
    "zoneCoolTemp",
    "zoneClimate",
    "outdoorTemp",
    "outdoorHumidity",
)
# Equipment seconds -> RuntimeInterval field. Stage-2 columns are stored as their own fields
# and NEVER added into stage-1 (stage-1 already includes stage-2 time).
_EQUIPMENT_FIELDS = {
    "auxHeat1": "aux_heat1",
    "auxHeat2": "aux_heat2",
    "compCool1": "comp_cool1",
    "compCool2": "comp_cool2",
    "compHeat1": "comp_heat1",
    "compHeat2": "comp_heat2",
    "fan": "fan",
}

# hold_type values (see the module docstring; the vocabulary is ``climate.sources.base``).
# The event types this app knows by name; a running event of any other type still overrides
# the program and keeps its type verbatim. ``PLAIN_HOLD_TYPES`` is re-exported from base.
EVENT_HOLD_TYPES = (*AUTO_EVENTS, *PERSON_EVENTS, *PROTECTED_EVENTS)
OVERRIDE_EVENT_TYPES = ("hold", *EVENT_HOLD_TYPES)
# Events listed in UnitSnapshot.events (running or ahead); never 'template' or past ones.
LISTED_EVENT_TYPES = ("demandResponse", "vacation")
NOT_AN_OVERRIDE = ("template",)  # ecobee's saved vacation templates are never in effect

_HVAC_MODES: dict[str, HvacMode] = {
    "heat": "heat",
    "cool": "cool",
    "auto": "auto",
    "off": "off",
    "auxheatonly": "auxHeatOnly",
    "emergencyheat": "auxHeatOnly",
}

# Unit auto-mapping by thermostat name: a word STARTING with one of these picks the unit.
_AUTOMAP_PREFIXES: dict[str, tuple[str, ...]] = {
    "main": ("hall", "main", "down"),
    "up": ("toy", "up"),
    "bed": ("bed",),
}


# --- scalars ---------------------------------------------------------------------------


def num(value: Any) -> float | None:
    """A plain number or None ('unknown', '', None, -5002, -5003, junk)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() == "unknown":
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out in (UNKNOWN_INT, CALIBRATING_INT):
        return None
    return out


def tenths_to_f(value: Any) -> float | None:
    """ecobee tenths of °F (int or numeric string) -> °F, None for unknown."""
    raw = num(value)
    return None if raw is None else round(raw / 10.0, 1)


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "1", "t", "yes"):
        return True
    if s in ("false", "0", "f", "no"):
        return False
    return None


def hvac_mode(value: Any) -> HvacMode:
    mode = _HVAC_MODES.get(str(value or "").strip().lower())
    if mode is None:
        log.warning("unknown ecobee hvacMode %r; treating as off", value)
        return "off"
    return mode


def parse_ecobee_dt(value: Any) -> datetime | None:
    """'YYYY-MM-DD HH:MM:SS' -> naive datetime (the caller knows its zone)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), _TS_FMT)  # noqa: DTZ007 - zone applied by the caller
    except ValueError:
        return None


def parse_utc(value: Any) -> datetime | None:
    naive = parse_ecobee_dt(value)
    return None if naive is None else naive.replace(tzinfo=UTC)


# --- thermostat-local time -------------------------------------------------------------


def thermostat_utc_offset(thermostat: dict[str, Any]) -> timedelta | None:
    """Current offset of thermostat-local time from UTC: ``thermostatTime - utcTime``,
    rounded to the nearest 15 minutes (the two stamps are taken a moment apart)."""
    local = parse_ecobee_dt(thermostat.get("thermostatTime"))
    utc = parse_ecobee_dt(thermostat.get("utcTime"))
    if local is None or utc is None:
        return None
    minutes = round((local - utc).total_seconds() / 900.0) * 15
    if abs(minutes) > 14 * 60:
        return None
    return timedelta(minutes=minutes)


def local_to_utc_offset(date_s: Any, time_s: Any, offset: timedelta) -> datetime | None:
    """Thermostat-local 'YYYY-MM-DD' + 'HH:MM:SS' -> UTC using a fixed offset (events)."""
    naive = parse_ecobee_dt(f"{date_s} {time_s}") if date_s and time_s else None
    if naive is None:
        return None
    return (naive - offset).replace(tzinfo=UTC)


def report_local_to_utc(date_s: str, time_s: str, tz: tzinfo) -> datetime | None:
    """THE runtimeReport timestamp conversion.

    Source: ecobee's RuntimeReport object documentation states that "the date/times returned in
    the runtime report are always in thermostat time" (mirrored verbatim in the go-ecobee client,
    sherif-fanous/go-ecobee objects/runtimereport.go), while the request's startDate /
    startInterval / endDate / endInterval are UTC (python Pyecobee converts the request range with
    ``astimezone(UTC)``; beestat converts each returned row with the thermostat's time zone).

    Each row's 'date,time' is a slot START in thermostat-local wall time. ``tz`` is the
    thermostat's IANA zone (``location.timeZone``), so a 31-day chunk that crosses a DST change
    converts correctly. Ambiguous fall-back times take the first occurrence (fold=0); wall times
    that do not exist (the spring-forward gap) return None and the row is dropped.
    """
    try:
        naive = datetime.strptime(f"{date_s.strip()} {time_s.strip()}", _TS_FMT)  # noqa: DTZ007 - local wall time
    except ValueError:
        return None
    utc = naive.replace(tzinfo=tz).astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != naive:
        return None  # nonexistent local time
    return utc


# --- /thermostatSummary ----------------------------------------------------------------


@dataclass(frozen=True)
class SummaryEntry:
    identifier: str
    name: str
    connected: bool
    thermostat_rev: str
    alerts_rev: str
    runtime_rev: str
    interval_rev: str
    equipment: tuple[str, ...] = ()

    @property
    def token(self) -> str:
        return revision_token(self.thermostat_rev, self.runtime_rev)


def revision_token(thermostat_rev: Any, runtime_rev: Any) -> str:
    """Per-unit change token: thermostatRev (program/settings/events) | runtimeRev (readings)."""
    return f"{thermostat_rev or ''}|{runtime_rev or ''}"


def parse_equipment(value: Any) -> list[str]:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def parse_summary(payload: dict[str, Any]) -> list[SummaryEntry]:
    """revisionList: 'identifier:name:connected:thermostatRev:alertsRev:runtimeRev:intervalRev'
    (a name containing ':' is rejoined); statusList: 'identifier:equipment,equipment'."""
    status: dict[str, tuple[str, ...]] = {}
    for line in payload.get("statusList") or []:
        ident, _, equip = str(line).partition(":")
        status[ident.strip()] = tuple(parse_equipment(equip))
    out: list[SummaryEntry] = []
    for line in payload.get("revisionList") or []:
        parts = str(line).split(":")
        if len(parts) < 7:
            log.warning("unparseable ecobee revisionList entry skipped")
            continue
        ident = parts[0].strip()
        name = ":".join(parts[1:-5])
        connected, t_rev, a_rev, r_rev, i_rev = parts[-5:]
        out.append(
            SummaryEntry(
                identifier=ident,
                name=name,
                connected=parse_bool(connected) is True,
                thermostat_rev=t_rev,
                alerts_rev=a_rev,
                runtime_rev=r_rev,
                interval_rev=i_rev,
                equipment=status.get(ident, ()),
            )
        )
    return out


def automap_candidates(name: str) -> set[str]:
    """Units a thermostat name points at: 'Hallway'/'Main Floor'/'Downstairs' -> main,
    'Toy Room'/'Upstairs' -> up, 'Bedroom' -> bed. Several (or none) means ambiguous."""
    words = [w for w in re.split(r"[^0-9a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower()) if w]
    found: set[str] = set()
    for unit, prefixes in _AUTOMAP_PREFIXES.items():
        if any(w.startswith(p) for w in words for p in prefixes):
            found.add(unit)
    return found


# --- sensors ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensorRef:
    """Our sensor row as the adapter needs it (``sensors`` table)."""

    key: str
    unit_key: str
    kind: str  # 'thermostat' | 'smartsensor'
    name: str
    ecobee_sensor_id: str | None = None


def base_sensor_id(sensor_id: str) -> str:
    """'rs:100:1' (sensor id + capability) -> 'rs:100'; 'ei:0' stays 'ei:0'."""
    parts = str(sensor_id).split(":")
    return ":".join(parts[:2]) if len(parts) >= 3 else str(sensor_id)


def _strip_sensor_word(n: str) -> str:
    return n[: -len("sensor")] if n.endswith("sensor") and len(n) > len("sensor") else n


def match_sensor(
    unit_key: str, ecobee_id: str, ecobee_name: str, ecobee_type: str | None, sensors: list[SensorRef]
) -> tuple[str | None, bool]:
    """Our sensor key for an ecobee sensor on ``unit_key``'s thermostat.

    Returns (key, by_name). Ecobee sensor ids are only unique per thermostat ('ei:0' is on
    every one), so matching is scoped to the unit. Order: an explicit ``ecobee_sensor_id``
    mapping; then the thermostat itself ('ei:0' / type 'thermostat') -> the unit's thermostat
    sensor; then a unique normalized-name match among the unit's SmartSensors (exact first,
    then ignoring a trailing 'sensor'). ``by_name`` is True when the caller should persist the
    id onto the sensor row.
    """
    mine = [s for s in sensors if s.unit_key == unit_key]
    for s in mine:
        if s.ecobee_sensor_id == ecobee_id:
            return s.key, False
    taken = {s.ecobee_sensor_id for s in mine if s.ecobee_sensor_id}
    if ecobee_id in taken:
        return None, False
    free = [s for s in mine if not s.ecobee_sensor_id]
    is_tstat = ecobee_id.startswith("ei:") or (ecobee_type or "") == "thermostat"
    if is_tstat:
        tstats = [s for s in free if s.kind == "thermostat"]
        return (tstats[0].key, True) if len(tstats) == 1 else (None, False)
    target = normalize_name(ecobee_name or "")
    if not target:
        return None, False
    smart = [s for s in free if s.kind != "thermostat"]
    exact = [s for s in smart if normalize_name(s.name) == target]
    if len(exact) == 1:
        return exact[0].key, True
    loose = [s for s in smart if _strip_sensor_word(normalize_name(s.name)) == _strip_sensor_word(target)]
    if len(loose) == 1:
        return loose[0].key, True
    return None, False


@dataclass
class SensorParse:
    readings: list[SensorReading] = field(default_factory=list)
    meta: list[dict[str, Any]] = field(default_factory=list)  # ecobee_thermostats.sensors
    id_to_key: dict[str, str] = field(default_factory=dict)  # ecobee base id -> our key
    new_mappings: dict[str, str] = field(default_factory=dict)  # our key -> ecobee id (by name)


def parse_remote_sensors(
    unit_key: str, remote_sensors: list[dict[str, Any]], sensors: list[SensorRef], ts: datetime
) -> SensorParse:
    out = SensorParse()
    working = list(sensors)
    for rs in remote_sensors or []:
        sid = str(rs.get("id") or "")
        if not sid:
            continue
        name = str(rs.get("name") or "")
        rtype = rs.get("type")
        caps: dict[str, Any] = {}
        for cap in rs.get("capability") or []:
            if isinstance(cap, dict) and cap.get("type"):
                caps[str(cap["type"])] = cap.get("value")
        key, by_name = match_sensor(unit_key, sid, name, rtype, working)
        if key and by_name:
            out.new_mappings[key] = sid
            working = [SensorRef(s.key, s.unit_key, s.kind, s.name, sid) if s.key == key else s for s in working]
        out.meta.append(
            {"id": sid, "name": name, "type": rtype, "capabilities": sorted(caps), "sensor_key": key,
             "in_use": rs.get("inUse")}
        )
        if key is None:
            continue
        out.id_to_key[sid] = key
        temp = tenths_to_f(caps.get("temperature")) if "temperature" in caps else None
        out.readings.append(
            SensorReading(
                sensor_key=key,
                ts=ts,
                temp_f=temp,
                humidity=num(caps.get("humidity")) if "humidity" in caps else None,
                occupied=parse_bool(caps.get("occupancy")) if "occupancy" in caps else None,
                # 'unknown' temperature = the sensor is not reporting (offline / out of range)
                online=not ("temperature" in caps and temp is None),
            )
        )
    return out


def parse_sensor_sets(program: dict[str, Any], id_to_key: dict[str, str]) -> dict[str, list[str]]:
    """program.climates[].sensors ({'id': 'rs:100:1', 'name': ...}) -> {climateRef: [our keys]}.
    Sensors we have not mapped are left out."""
    sets: dict[str, list[str]] = {}
    for climate in program.get("climates") or []:
        ref = climate.get("climateRef")
        if not ref:
            continue
        keys: list[str] = []
        for s in climate.get("sensors") or []:
            key = id_to_key.get(base_sensor_id(str(s.get("id") or "")))
            if key and key not in keys:
                keys.append(key)
        sets[str(ref)] = keys
    return sets


def program_setpoints(program: dict[str, Any]) -> tuple[float | None, float | None]:
    """(heat °F, cool °F) of the program's current climate (heatTemp/coolTemp are tenths)."""
    ref = program.get("currentClimateRef")
    for climate in program.get("climates") or []:
        if ref and climate.get("climateRef") == ref:
            return tenths_to_f(climate.get("heatTemp")), tenths_to_f(climate.get("coolTemp"))
    return None, None


def climate_sensor_ids(program: dict[str, Any]) -> dict[str, list[str]]:
    """{climateRef: [base sensor ids]} straight from the program (for read-backs)."""
    out: dict[str, list[str]] = {}
    for climate in program.get("climates") or []:
        ref = climate.get("climateRef")
        if ref:
            out[str(ref)] = [base_sensor_id(str(s.get("id") or "")) for s in climate.get("sensors") or []]
    return out


# --- settings, weather -----------------------------------------------------------------


def curated_settings(settings: dict[str, Any]) -> dict[str, object]:
    """The thermostat settings the app uses. ``drAccept`` is the utility-event enrollment
    choice (always / askMe / customerSelect / defaultAccept / defaultDecline / never)."""
    out: dict[str, object] = {}
    for key in ("autoAway", "followMeComfort", "hvacMode", "hasHeatPump", "heatStages", "coolStages",
                "useCelsius", "fanMinOnTime", "holdAction", "smartCirculation", "drAccept"):
        if key in settings:
            out[key] = settings[key]
    for key in ("heatCoolMinDelta", "heatRangeHigh", "heatRangeLow", "coolRangeHigh", "coolRangeLow"):
        if key in settings:
            out[key] = tenths_to_f(settings[key])
    return out


def outdoor_now(weather: dict[str, Any] | None) -> tuple[float | None, float | None]:
    forecasts = (weather or {}).get("forecasts") or []
    if not forecasts:
        return None, None
    first = forecasts[0]
    return tenths_to_f(first.get("temperature")), num(first.get("relativeHumidity"))


def _text(value: Any) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


def parse_utility(obj: dict[str, Any] | None) -> UtilityInfo | None:
    """``includeUtility``'s Utility object -> UtilityInfo; None when no utility is named
    (ecobee returns the object with empty strings for a thermostat with no utility)."""
    if not isinstance(obj, dict):
        return None
    name = _text(obj.get("name"))
    if name is None:
        return None
    return UtilityInfo(name=name, phone=_text(obj.get("phone")), email=_text(obj.get("email")),
                       web=_text(obj.get("web")))


# --- events / holds --------------------------------------------------------------------


def running_override(events: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The event in effect: the first RUNNING event of any type (ecobee orders the list by
    running state and priority), never a vacation template."""
    for ev in events or []:
        if not isinstance(ev, dict) or str(ev.get("type") or "") in NOT_AN_OVERRIDE:
            continue
        if parse_bool(ev.get("running")):
            return ev
    return None


def is_event_hold(hold: HoldInfo | None) -> bool:
    """True when what runs is an event (any type, known or not) rather than a plain hold."""
    return hold is not None and hold.hold_type not in PLAIN_HOLD_TYPES


@dataclass(frozen=True)
class _EventDetail:
    """The setpoint change an ecobee event makes (see the module docstring)."""

    heat_f: float | None
    cool_f: float | None
    is_relative: bool
    heat_offset_f: float | None
    cool_offset_f: float | None
    is_optional: bool | None
    is_cool_off: bool
    is_heat_off: bool
    duty_cycle_pct: int | None
    link_ref: str | None
    name: str | None


def _offset(value: Any, sign: float) -> float | None:
    """A relative temperature (tenths, a setback magnitude) -> signed °F; None for no change."""
    f = tenths_to_f(value)
    return None if not f else round(sign * abs(f), 1)


def _event_detail(event: dict[str, Any]) -> _EventDetail:
    relative = parse_bool(event.get("isTemperatureRelative")) is True
    absolute = parse_bool(event.get("isTemperatureAbsolute"))
    if absolute is None:
        absolute = not relative  # a relative event's hold temps are not setpoints
    duty = num(event.get("dutyCyclePercentage"))
    return _EventDetail(
        heat_f=tenths_to_f(event.get("heatHoldTemp")) if absolute else None,
        cool_f=tenths_to_f(event.get("coolHoldTemp")) if absolute else None,
        is_relative=relative,
        heat_offset_f=_offset(event.get("heatRelativeTemp"), -1.0) if relative else None,
        cool_offset_f=_offset(event.get("coolRelativeTemp"), 1.0) if relative else None,
        is_optional=parse_bool(event.get("isOptional")),
        is_cool_off=parse_bool(event.get("isCoolOff")) is True,
        is_heat_off=parse_bool(event.get("isHeatOff")) is True,
        duty_cycle_pct=int(duty) if duty is not None and 0 <= duty <= 100 else None,
        link_ref=_text(event.get("linkRef")),
        name=_text(event.get("name")),
    )


def _event_time(date_s: Any, time_s: Any, offset: timedelta, tz: tzinfo | None) -> datetime | None:
    """Thermostat-local event date + time -> UTC: through the thermostat's zone when known (an
    event weeks ahead may sit across a DST change), else with the current offset."""
    if tz is not None and date_s and time_s:
        utc = report_local_to_utc(str(date_s), str(time_s), tz)
        if utc is not None:
            return utc
    return local_to_utc_offset(date_s, time_s, offset)


def parse_events(
    events: list[dict[str, Any]] | None, offset: timedelta | None, now: datetime | None = None,
    tz: tzinfo | None = None,
) -> list[ThermostatEvent]:
    """Demand-response and vacation events that are running or still ahead -> ThermostatEvent,
    in ecobee's order. Times go to UTC through ``tz`` (the thermostat's location.timeZone)
    when given, else the current ``offset``. Templates, other types and events whose end has
    passed (``now``: the thermostat's own utcTime; default the current time) are left out. An
    event with no end is kept while running or not started."""
    off = offset or timedelta(0)
    now = now or datetime.now(UTC)
    out: list[ThermostatEvent] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "")
        if etype not in LISTED_EVENT_TYPES:
            continue
        running = parse_bool(ev.get("running")) is True
        start = _event_time(ev.get("startDate"), ev.get("startTime"), off, tz)
        end = _event_time(ev.get("endDate"), ev.get("endTime"), off, tz)
        if not running:
            if end is not None and end <= now:
                continue  # over
            if end is None and (start is None or start <= now):
                continue  # no end and not ahead: nothing to show
        d = _event_detail(ev)
        out.append(ThermostatEvent(
            event_type=etype, name=d.name, running=running, start=start, end=end, heat_f=d.heat_f,
            cool_f=d.cool_f, is_relative=d.is_relative, heat_offset_f=d.heat_offset_f,
            cool_offset_f=d.cool_offset_f, is_optional=d.is_optional, is_cool_off=d.is_cool_off,
            is_heat_off=d.is_heat_off, duty_cycle_pct=d.duty_cycle_pct, link_ref=d.link_ref,
        ))
    return out


def next_transition_local(program: dict[str, Any], after_local: datetime) -> datetime | None:
    """Start of the next program block whose climate differs from the one at ``after_local``.

    ASSUMPTION (ecobee Program object docs, not re-verified here): ``schedule`` is 7 rows,
    Monday first, of 48 half-hour climateRefs."""
    schedule = program.get("schedule") or []
    if len(schedule) != 7 or any(len(day or []) != 48 for day in schedule):
        return None
    day = after_local.weekday()
    slot = (after_local.hour * 60 + after_local.minute) // 30
    current = schedule[day][slot]
    midnight = after_local.replace(hour=0, minute=0, second=0, microsecond=0)
    for step in range(1, 7 * 48 + 1):
        idx = slot + step
        if schedule[(day + idx // 48) % 7][idx % 48] != current:
            return midnight + timedelta(minutes=30 * idx)
    return None


def hold_matches_ours(hold: HoldInfo, ours: dict[str, Any] | None, tol_f: float = 0.1) -> bool:
    """True when ``hold`` is the temperature hold the controller last wrote (and read back)."""
    if not ours or hold.kind != "temperature" or hold.heat_f is None or hold.cool_f is None:
        return False
    try:
        if abs(hold.heat_f - float(ours["heat_f"])) > tol_f or abs(hold.cool_f - float(ours["cool_f"])) > tol_f:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    our_end = ours.get("end")
    if our_end and hold.end is not None:
        end = datetime.fromisoformat(str(our_end))
        if abs((hold.end - end).total_seconds()) > 300:
            return False
    return True


def parse_hold(
    event: dict[str, Any], offset: timedelta | None, program: dict[str, Any] | None, ours: dict[str, Any] | None
) -> HoldInfo:
    """A running override event -> HoldInfo (start/end converted to UTC with the thermostat's
    current offset). ``hold_type`` for a 'hold' event is inferred, because ecobee does not echo
    the holdType: ends in 2035+ -> indefinite; matches our last write -> holdHours; ends on the
    next program transition after it started -> nextTransition; a whole number of hours ->
    holdHours; otherwise dateTime. Any other running event reports its ecobee type verbatim
    (vacation / autoAway / autoHome / quickSave / demandResponse, or an unknown one), is never
    ``set_by_us``, and carries the event details (name, relative offsets, isOptional, linkRef);
    its absolute setpoints are filled only when the event is absolute."""
    off = offset or timedelta(0)
    start = local_to_utc_offset(event.get("startDate"), event.get("startTime"), off)
    end = local_to_utc_offset(event.get("endDate"), event.get("endTime"), off)
    ref = str(event.get("holdClimateRef") or "") or None
    etype = str(event.get("type") or "hold")
    d = _event_detail(event)
    hold = HoldInfo(kind="climate" if ref else "temperature", heat_f=d.heat_f, cool_f=d.cool_f,
                    climate_ref=ref, start=start, end=end, hold_type=etype, set_by_us=False)
    if etype != "hold":
        # an event, never ours (even if its setpoints equal our last hold)
        hold.event_name = d.name
        hold.is_relative = d.is_relative
        hold.heat_offset_f = d.heat_offset_f
        hold.cool_offset_f = d.cool_offset_f
        hold.is_optional = d.is_optional
        hold.link_ref = d.link_ref
        return hold
    hold.set_by_us = hold_matches_ours(hold, ours)
    end_local = parse_ecobee_dt(f"{event.get('endDate')} {event.get('endTime')}")
    start_local = parse_ecobee_dt(f"{event.get('startDate')} {event.get('startTime')}")
    if end_local is not None and end_local.year >= 2035:
        hold.hold_type = "indefinite"
    elif hold.set_by_us:
        hold.hold_type = "holdHours"
    else:
        nxt = next_transition_local(program or {}, start_local) if start_local else None
        if nxt is not None and end_local is not None and abs((end_local - nxt).total_seconds()) <= 120:
            hold.hold_type = "nextTransition"
        elif start_local is not None and end_local is not None and _whole_hours(end_local - start_local):
            hold.hold_type = "holdHours"
        else:
            hold.hold_type = "dateTime"
    return hold


def _whole_hours(d: timedelta) -> bool:
    secs = d.total_seconds()
    if secs < 3600 - 120 or secs > 24 * 3600 + 120:
        return False
    return abs(secs - round(secs / 3600) * 3600) <= 120


# --- /runtimeReport --------------------------------------------------------------------


def climate_ref_of(value: str | None, names: dict[str, str]) -> str | None:
    """runtimeReport ``zoneClimate`` -> climateRef. ASSUMPTION: the column carries the comfort
    setting's display NAME ('Home'); a name -> ref map from the last program read resolves custom
    ones, and the built-in names fall back to their fixed refs (home / away / sleep)."""
    if not value:
        return None
    if value in names:
        return names[value]
    if value.lower() in ("home", "away", "sleep"):
        return value.lower()
    return value


def _int_seconds(value: str) -> int:
    n = num(value)
    if n is None:
        return 0
    return max(0, min(300, round(n)))


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in str(row).split(",")]


def parse_runtime_report(
    payload: dict[str, Any],
    units_by_ident: dict[str, str],
    sensors: list[SensorRef],
    tz_by_ident: dict[str, tzinfo],
    start: datetime,
    end: datetime,
    climate_refs_by_ident: dict[str, dict[str, str]] | None = None,
) -> list[RuntimeInterval]:
    """reportList rowList + sensorList data -> one RuntimeInterval per unit and 5-minute slot in
    [start, end). Blank equipment cells -> 0, blank readings -> None; rows with no data at all
    (report lag, thermostat offline) are skipped rather than recorded as zero runtime."""
    columns = [c.strip() for c in str(payload.get("columns") or ",".join(REPORT_COLUMNS)).split(",") if c.strip()]
    climate_refs_by_ident = climate_refs_by_ident or {}
    intervals: dict[tuple[str, datetime], RuntimeInterval] = {}

    for report in payload.get("reportList") or []:
        ident = str(report.get("thermostatIdentifier") or "")
        unit = units_by_ident.get(ident)
        tz = tz_by_ident.get(ident)
        if unit is None or tz is None:
            continue
        names = climate_refs_by_ident.get(ident, {})
        for row in report.get("rowList") or []:
            cells = _row_cells(row)
            if len(cells) != 2 + len(columns):
                log.debug("runtimeReport row with %d cells skipped (expected %d)", len(cells), 2 + len(columns))
                continue
            values = dict(zip(columns, cells[2:], strict=True))
            if not any(values.values()):
                continue
            ts = report_local_to_utc(cells[0], cells[1], tz)
            if ts is None or not (start <= ts < end):
                continue
            iv = RuntimeInterval(unit_key=unit, ts=ts)
            for col, fld in _EQUIPMENT_FIELDS.items():
                if col in values:
                    setattr(iv, fld, _int_seconds(values[col]))
            iv.hvac_mode = values.get("hvacMode") or None
            iv.climate_ref = climate_ref_of(values.get("zoneClimate") or None, names)
            iv.zone_temp_f = num(values.get("zoneAveTemp"))
            iv.zone_humidity = num(values.get("zoneHumidity"))
            iv.heat_sp_f = num(values.get("zoneHeatTemp"))
            iv.cool_sp_f = num(values.get("zoneCoolTemp"))
            iv.outdoor_temp_f = num(values.get("outdoorTemp"))
            iv.outdoor_humidity = num(values.get("outdoorHumidity"))
            intervals[(unit, ts)] = iv

    for block in payload.get("sensorList") or []:
        ident = str(block.get("thermostatIdentifier") or "")
        unit = units_by_ident.get(ident)
        tz = tz_by_ident.get(ident)
        if unit is None or tz is None:
            continue
        meta = {str(s.get("sensorId")): s for s in block.get("sensors") or []}
        cols = [str(c) for c in block.get("columns") or []]
        # column index -> (our sensor key, 'temperature' | 'occupancy')
        targets: dict[int, tuple[str, str]] = {}
        working = list(sensors)
        for i, col in enumerate(cols):
            if col in ("date", "time") or col not in meta:
                continue
            stype = str(meta[col].get("sensorType") or "")
            if stype not in ("temperature", "occupancy"):
                continue
            base = base_sensor_id(col)
            key, by_name = match_sensor(unit, base, str(meta[col].get("sensorName") or ""), None, working)
            if key is None:
                continue
            if by_name:
                working = [SensorRef(s.key, s.unit_key, s.kind, s.name, base) if s.key == key else s for s in working]
            targets[i] = (key, stype)
        if not targets:
            continue
        for row in block.get("data") or []:
            cells = _row_cells(row)
            if len(cells) != len(cols) or len(cells) < 2:
                continue
            ts = report_local_to_utc(cells[0], cells[1], tz)
            iv = intervals.get((unit, ts)) if ts is not None else None
            if iv is None:
                continue
            for i, (key, stype) in targets.items():
                if stype == "temperature":
                    iv.sensor_temps[key] = num(cells[i])
                else:
                    iv.sensor_occupancy[key] = parse_bool(cells[i]) if cells[i] else None

    return [intervals[k] for k in sorted(intervals, key=lambda k: (k[1], k[0]))]


def report_chunks(start: datetime, end: datetime, max_days: int = 30) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into inclusive (first_slot, last_slot) UTC pairs, each spanning at
    most ``max_days`` days (ecobee allows <= 31 days per runtimeReport request)."""
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    first = start - timedelta(minutes=start.minute % 5, seconds=start.second, microseconds=start.microsecond)
    last_point = end - timedelta(microseconds=1)
    last = last_point - timedelta(
        minutes=last_point.minute % 5, seconds=last_point.second, microseconds=last_point.microsecond
    )
    chunks: list[tuple[datetime, datetime]] = []
    cur = first
    span = timedelta(days=max_days) - timedelta(minutes=5)
    while cur <= last:
        chunk_last = min(last, cur + span)
        chunks.append((cur, chunk_last))
        cur = chunk_last + timedelta(minutes=5)
    return chunks


def report_request(identifiers: list[str], first: datetime, last: datetime) -> dict[str, Any]:
    """runtimeReport body. Dates and intervals (0..287, 5 minutes each) are UTC; both ends inclusive."""
    return {
        "selection": {"selectionType": "thermostats", "selectionMatch": ",".join(identifiers)},
        "startDate": first.strftime("%Y-%m-%d"),
        "startInterval": first.hour * 12 + first.minute // 5,
        "endDate": last.strftime("%Y-%m-%d"),
        "endInterval": last.hour * 12 + last.minute // 5,
        "columns": ",".join(REPORT_COLUMNS),
        "includeSensors": True,
    }

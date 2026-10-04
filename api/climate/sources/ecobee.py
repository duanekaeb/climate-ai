"""The ONE ecobee cloud adapter (CLAUDE.md). Everything ecobee-auth lives behind this file.

Sign-in uses python-ecobee-api 0.4.x account sign-in (Auth0 PKCE web flow, TOTP/SMS MFA)
only for login; token refresh is a direct refresh-token grant against auth.ecobee.com, and every
data call is made here with the bearer token: /1/thermostatSummary (every >= 3 min),
/1/thermostat (on revision change), /1/runtimeReport (<= 31 days per call, one at a time), and
setHold/resumeProgram/settings/program writes, each read back (the library swallows HTTP
errors, so it is never used for data or writes). ``resumeProgram`` cancels a plain hold
(``resume_program``; the owner's forced one also a Quick Save or Smart Away / Home) or, with a
utility event on top, opts out of it (``opt_out_event``); each refuses the other's case. Every
write decides on a fresh GET of what runs on top, never a cached detail: a controller write
never replaces a hold a person set since the controller's last poll (``base.write_refusal``).
Rules:
- The web client id comes from settings (``CLIMATE_ECOBEE_WEB_CLIENT_ID``) and is patched into
  pyecobee before use (its functions read the module global ``pyecobee.ECOBEE_WEB_CLIENT_ID`` at
  call time); the account route is unofficial and has changed before.
- Persist the NEWEST refresh token (encrypted, secrets 'ecobee_refresh_token') after every
  refresh (Auth0 may rotate it, and reusing a rotated token can revoke the grant); never store
  the password; only the worker refreshes (the API process only signs in). Refreshes are
  serialized with an asyncio.Lock and always start from the stored token, so a fresh sign-in
  from the API process is picked up by the worker on its next refresh.
- MFA: authenticator-app (TOTP) and SMS codes only. Push and email verification are NOT
  supported by the library; the owner must switch the account to TOTP or SMS.
- Keep the 'pyecobee' logger at INFO or above (it logs tokens at DEBUG). Nothing here logs a
  token, a password or a request body.
- holdType 'holdHours' with holdHours 1-2; temperatures rounded to 0.5°F then x10 ints.
- Program edits start from a fresh GET and a revision check (thermostatRev must not move
  between the read and the write).
- Keeping the Home comfort setting's sensor set current (temperature holds use Home's sensors)
  is the CALLER's job: ``update_sensor_sets`` writes exactly the climates it is given.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pyecobee
import pyecobee.const
from cryptography.fernet import Fernet
from pyecobee.errors import EcobeeAuthFailedError, EcobeeAuthMfaRequiredError, EcobeeAuthUnknownError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.config import get_settings
from climate.house import UNIT_KEYS
from climate.sources import ecobee_parse as P
from climate.sources.base import (
    AUTO_EVENTS,
    NOT_OURS_ERROR,
    PERSON_EVENTS,
    HoldInfo,
    HoldRequest,
    RuntimeInterval,
    SourceHealth,
    UnitSnapshot,
    WriteResult,
    event_identity,
    write_refusal,
)
from climate.store.app_settings import LocationSettings, get_raw, get_setting, put_setting
from climate.store.db import session_scope
from climate.store.orm import AppSetting, EcobeeThermostat, Sensor, Unit
from climate.store.secrets import SecretsUnavailable, delete_secret, get_secret, put_secret
from climate.timeutil import utcnow

log = logging.getLogger("climate.ecobee")

API_BASE = "https://api.ecobee.com/1"
TOKEN_URL = "https://auth.ecobee.com/oauth/token"
REFRESH_SECRET = "ecobee_refresh_token"
STATUS_KEY = "ecobee_status"  # {signed_in_at, last_error, error_at}
# {unit_key: {heat_f, cool_f, end, hours, written_at[, by_owner][, attempt]}}: the last hold we
# wrote and read back (``by_owner`` for the owner's hold from the app, which is never ours), and
# ``attempt`` {heat_f, cool_f, end_from, end_to, sent_at}: a newer controller write whose
# read-back failed (it may have landed; a hold matching it still reads as ours)
HOLDS_KEY = "ecobee_holds"
MFA_TTL = timedelta(minutes=10)
HTTP_TIMEOUT_S = 30.0
HOLD_END_SLACK = timedelta(minutes=15)  # a verified hold ends no later than now + hours + this
RENEWAL_SLACK = timedelta(minutes=10)  # ...and no earlier than (sent, thermostat clock) + hours - this
OPT_OUT_END_GUARD = timedelta(minutes=2)  # no opt-out this close to an event's end (see opt_out_event)
WANTED_SETTINGS: dict[str, bool] = {"autoAway": False, "followMeComfort": False}
SETTABLE_SETTINGS = tuple(WANTED_SETTINGS)  # the only settings apply_settings writes
ENSURE_SETTINGS_REASON = "daily check: Smart Home/Away and Follow Me stay off (the controller owns occupancy)"

SUMMARY_SELECTION: dict[str, Any] = {
    "selectionType": "registered",
    "selectionMatch": "",
    "includeEquipmentStatus": True,
}
DETAIL_INCLUDES: dict[str, bool] = {
    "includeRuntime": True,
    "includeExtendedRuntime": True,
    "includeSensors": True,
    "includeProgram": True,
    "includeEvents": True,
    "includeSettings": True,
    "includeEquipmentStatus": True,
    "includeWeather": True,
    "includeLocation": True,  # only location.timeZone is kept (runtimeReport rows are local time)
    "includeUtility": True,  # the utility the thermostat is enrolled with (Setup shows it)
}
READBACK_INCLUDES: dict[str, bool] = {
    "includeRuntime": True,
    "includeEvents": True,
    "includeSettings": True,
    "includeProgram": True,
}
# The opt-out's fresh read also takes location.timeZone: an event's identity (name + start, when
# it has no linkRef) must convert exactly as the snapshot that listed it did.
OPT_OUT_INCLUDES: dict[str, bool] = {**READBACK_INCLUDES, "includeLocation": True}
# Sensor-set writes read the program, the sensors and what runs on top (a person's hold refuses).
PROGRAM_INCLUDES: dict[str, bool] = {"includeProgram": True, "includeSensors": True, "includeEvents": True}

_UNSUPPORTED_MFA = (
    "This ecobee account uses push or email two-step verification, which sign-in here does not "
    "support (only authenticator-app (TOTP) and SMS codes work). In the ecobee app, switch "
    "two-step verification to an authenticator app or SMS, then sign in again."
)

T = TypeVar("T")


def _quiet_library_logger() -> None:
    """pyecobee logs tokens, passwords and request bodies at DEBUG: keep it at INFO or above."""
    lib = logging.getLogger("pyecobee")
    if lib.getEffectiveLevel() < logging.INFO or lib.level < logging.INFO:
        lib.setLevel(logging.INFO)


_quiet_library_logger()


class EcobeeAuthError(RuntimeError):
    """Sign-in or refresh failed; the owner must sign in again."""


class EcobeeApiError(RuntimeError):
    """An ecobee call failed (network, HTTP error or a non-zero ecobee status code).

    ``transport`` is True when no response arrived (the request may or may not have landed)."""

    def __init__(self, message: str, *, status_code: int | None = None, ecobee_code: int | None = None,
                 transport: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.ecobee_code = ecobee_code
        self.transport = transport


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _status_of(payload: Any) -> tuple[int | None, str]:
    if not isinstance(payload, dict):
        return None, ""
    status = payload.get("status")
    if not isinstance(status, dict):
        return None, ""
    code = status.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, str(status.get("message") or "")[:200]


def _zone(name: Any) -> tzinfo | None:
    if not name or not isinstance(name, str):
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


async def _db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``fn(session, *args, **kwargs)`` in its own transaction on a worker thread."""

    def run() -> T:
        with session_scope() as session:
            return fn(session, *args, **kwargs)

    return await asyncio.to_thread(run)


def _merge_status(session: Session, **fields: Any) -> None:
    raw = get_raw(session, STATUS_KEY)
    data = dict(raw) if isinstance(raw, dict) else {}
    data.update(fields)
    put_setting(session, STATUS_KEY, data, updated_by="ecobee")


def login_status(session: Session) -> dict[str, Any]:
    """{'signed_in': refresh token stored, 'signed_in_at', 'last_error'} for the setup screen."""
    try:
        signed_in = get_secret(session, REFRESH_SECRET) is not None
    except SecretsUnavailable:
        signed_in = False
    raw = get_raw(session, STATUS_KEY)
    data = raw if isinstance(raw, dict) else {}
    return {"signed_in": signed_in, "signed_in_at": data.get("signed_in_at"), "last_error": data.get("last_error")}


# --- inventory (units / sensors / thermostats) -----------------------------------------


@dataclass
class _Inventory:
    unit_by_ident: dict[str, str]
    ident_by_unit: dict[str, str]
    sensors: list[P.SensorRef]
    thermostat_settings: dict[str, dict[str, Any]]
    our_holds: dict[str, dict[str, Any]]
    house_tz: str


def _load_inventory(session: Session) -> _Inventory:
    units = session.execute(select(Unit.key, Unit.ecobee_identifier)).all()
    ident_by_unit = {k: i for k, i in units if i}
    sensors = [
        P.SensorRef(key=r.key, unit_key=r.unit_key, kind=r.kind, name=r.name, ecobee_sensor_id=r.ecobee_sensor_id)
        for r in session.execute(
            select(Sensor.key, Sensor.unit_key, Sensor.kind, Sensor.name, Sensor.ecobee_sensor_id).order_by(Sensor.sort)
        ).all()
    ]
    tstats = {i: (s or {}) for i, s in session.execute(select(EcobeeThermostat.identifier, EcobeeThermostat.settings))}
    holds_raw = get_raw(session, HOLDS_KEY)
    holds = holds_raw if isinstance(holds_raw, dict) else {}
    tz = get_setting(session, "location", LocationSettings).tz
    return _Inventory(
        unit_by_ident={i: k for k, i in ident_by_unit.items()},
        ident_by_unit=ident_by_unit,
        sensors=sensors,
        thermostat_settings=tstats,
        our_holds=holds,
        house_tz=tz,
    )


@dataclass
class _DetailUpdate:
    identifier: str
    unit_key: str
    name: str
    model_number: str | None
    sensors_meta: list[dict[str, Any]]
    settings: dict[str, Any]
    revision: str
    new_mappings: dict[str, str] = field(default_factory=dict)


def _save_mappings(session: Session, unit_key: str, mappings: dict[str, str]) -> None:
    for key, sid in mappings.items():
        session.execute(
            update(Sensor)
            .where(Sensor.key == key, Sensor.unit_key == unit_key, Sensor.ecobee_sensor_id.is_(None))
            .values(ecobee_sensor_id=sid)
        )
        log.info("ecobee sensor %s on unit %s mapped to %s by name", sid, unit_key, key)


def _persist_details(session: Session, updates: list[_DetailUpdate]) -> None:
    now = utcnow()
    for u in updates:
        stmt = insert(EcobeeThermostat).values(
            identifier=u.identifier, name=u.name, model_number=u.model_number, unit_key=u.unit_key,
            sensors=u.sensors_meta, settings=u.settings, last_revision=u.revision, last_seen_at=now,
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[EcobeeThermostat.identifier],
                set_={"name": u.name, "model_number": u.model_number, "unit_key": u.unit_key,
                      "sensors": u.sensors_meta, "settings": u.settings, "last_revision": u.revision,
                      "last_seen_at": now},
            )
        )
        if u.model_number:
            session.execute(update(Unit).where(Unit.key == u.unit_key).values(thermostat_model=u.model_number))
        _save_mappings(session, u.unit_key, u.new_mappings)


def _sync_summary(session: Session, entries: list[P.SummaryEntry]) -> dict[str, str]:
    """Upsert ecobee_thermostats from the summary; auto-map NEW thermostats by name.

    Auto-mapping happens only when a thermostat is first discovered, its name points at exactly
    one unit, that unit has no thermostat yet, and no other new thermostat claims the same unit.
    An owner's later (un)mapping in Setup therefore always sticks."""
    now = utcnow()
    units = list(session.execute(select(Unit)).scalars())
    unit_by_ident = {u.ecobee_identifier: u.key for u in units if u.ecobee_identifier}
    idents = [e.identifier for e in entries]
    known = set(session.execute(select(EcobeeThermostat.identifier).where(EcobeeThermostat.identifier.in_(idents))).scalars())
    proposals: dict[str, str] = {}
    for e in entries:
        if e.identifier in unit_by_ident or e.identifier in known:
            continue
        cands = P.automap_candidates(e.name)
        if len(cands) == 1:
            proposals[e.identifier] = next(iter(cands))
    claims = Counter(proposals.values())
    by_key = {u.key: u for u in units}
    for ident, unit_key in proposals.items():
        unit = by_key.get(unit_key)
        if unit is None or unit.ecobee_identifier or claims[unit_key] != 1:
            continue
        unit.ecobee_identifier = ident
        unit_by_ident[ident] = unit_key
        log.info("ecobee thermostat %s auto-mapped to unit %s by name", ident, unit_key)
    session.flush()
    for e in entries:
        stmt = insert(EcobeeThermostat).values(
            identifier=e.identifier, name=e.name, unit_key=unit_by_ident.get(e.identifier),
            last_revision=e.token, last_seen_at=now,
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[EcobeeThermostat.identifier],
                set_={"name": e.name, "unit_key": unit_by_ident.get(e.identifier), "last_revision": e.token,
                      "last_seen_at": now},
            )
        )
    return unit_by_ident


def _set_tz(session: Session, ident: str, tz_name: str) -> None:
    row = session.get(EcobeeThermostat, ident)
    if row is not None:
        row.settings = {**(row.settings or {}), "timeZone": tz_name}


def _record_hold(session: Session, unit_key: str, record: dict[str, Any] | None) -> None:
    raw = get_raw(session, HOLDS_KEY)
    holds = dict(raw) if isinstance(raw, dict) else {}
    if record is None:
        holds.pop(unit_key, None)
    else:
        holds[unit_key] = record
    put_setting(session, HOLDS_KEY, holds, updated_by="ecobee")


def _record_attempt(session: Session, unit_key: str, attempt: dict[str, Any]) -> None:
    """Note a controller write that went out but did not verify next to the record of our last
    verified hold (which stays as it is): if it landed, a fresh read still sees it as ours."""
    raw = get_raw(session, HOLDS_KEY)
    holds = dict(raw) if isinstance(raw, dict) else {}
    current = holds.get(unit_key)
    holds[unit_key] = {**(current if isinstance(current, dict) else {}), "attempt": attempt}
    put_setting(session, HOLDS_KEY, holds, updated_by="ecobee")


# --- building our types from a thermostat object ---------------------------------------


def _hold_of(t: dict[str, Any], ours: dict[str, Any] | None, tz: tzinfo | None = None) -> HoldInfo | None:
    """The running top override as HoldInfo; an event's times convert through ``tz`` when known
    (``ecobee_parse.parse_hold``)."""
    ev = P.running_override(t.get("events"))
    if ev is None:
        return None
    return P.parse_hold(ev, P.thermostat_utc_offset(t), t.get("program") or {}, ours, tz)


def _write_state(t: dict[str, Any], ours: dict[str, Any] | None, tz: tzinfo | None = None) -> dict[str, object]:
    """Compact state for WriteResult.before / readback."""
    runtime = t.get("runtime") or {}
    settings = t.get("settings") or {}
    program = t.get("program") or {}
    hold = _hold_of(t, ours, tz)
    return {
        "identifier": t.get("identifier"),
        "revision": P.revision_token(t.get("thermostatRev"), runtime.get("runtimeRev")),
        "hvac_mode": settings.get("hvacMode"),
        "climate_ref": program.get("currentClimateRef"),
        "heat_sp_f": P.tenths_to_f(runtime.get("desiredHeat")),
        "cool_sp_f": P.tenths_to_f(runtime.get("desiredCool")),
        "hold": hold.model_dump(mode="json") if hold else None,
    }


def _snapshot_from(
    unit_key: str, t: dict[str, Any], sensors: list[P.SensorRef], ours: dict[str, Any] | None, now: datetime
) -> tuple[UnitSnapshot, _DetailUpdate]:
    runtime = t.get("runtime") or {}
    extended = t.get("extendedRuntime") or {}
    settings = t.get("settings") or {}
    program = t.get("program") or {}
    ts = P.parse_utc(t.get("utcTime")) or now
    offset = P.thermostat_utc_offset(t)
    tz_name = (t.get("location") or {}).get("timeZone")
    tz = _zone(tz_name)
    sp = P.parse_remote_sensors(unit_key, t.get("remoteSensors") or [], sensors, ts)
    hold = _hold_of(t, ours, tz)  # an event's times through the zone, like ``events`` below
    outdoor_t, outdoor_h = P.outdoor_now(t.get("weather"))
    zone_t = P.tenths_to_f(runtime.get("actualTemperature"))
    if zone_t is None and extended.get("actualTemperature"):
        zone_t = P.tenths_to_f(extended["actualTemperature"][-1])
    snap_settings: dict[str, object] = dict(P.curated_settings(settings))
    # The schedule's own setpoints for the climate in force (program.currentClimateRef stays the
    # scheduled climate during a hold), so the controller can resume a hold of ours early.
    prog_heat, prog_cool = P.program_setpoints(program)
    if prog_heat is not None and prog_cool is not None:
        snap_settings["program_heat_f"] = prog_heat
        snap_settings["program_cool_f"] = prog_cool
    if tz_name:
        snap_settings["timeZone"] = tz_name
    if offset is not None:
        snap_settings["utcOffsetMinutes"] = int(offset.total_seconds() // 60)
    revision = P.revision_token(t.get("thermostatRev"), runtime.get("runtimeRev"))
    model = t.get("modelNumber") or None
    name = str(t.get("name") or t.get("identifier"))
    utility = P.parse_utility(t.get("utility"))
    snap = UnitSnapshot(
        unit_key=unit_key,
        ts=ts,
        source="ecobee",
        revision=revision,
        name=name,
        model=model,
        hvac_mode=P.hvac_mode(settings.get("hvacMode")),
        equipment_running=P.parse_equipment(t.get("equipmentStatus")),
        heat_sp_f=P.tenths_to_f(runtime.get("desiredHeat")),
        cool_sp_f=P.tenths_to_f(runtime.get("desiredCool")),
        climate_ref=program.get("currentClimateRef") or None,
        hold=hold,
        zone_temp_f=zone_t,
        zone_humidity=P.num(runtime.get("actualHumidity")),
        outdoor_temp_f=outdoor_t,
        outdoor_humidity=outdoor_h,
        sensors=sp.readings,
        sensor_sets=P.parse_sensor_sets(program, sp.id_to_key),
        settings=snap_settings,
        connected=P.parse_bool(runtime.get("connected")) is not False,
        events=P.parse_events(t.get("events"), offset, now=ts, tz=tz),
        utility=utility,
    )
    # ecobee_thermostats.settings also keeps the utility (or null) for Setup's enrollment view;
    # drAccept is already among the curated settings.
    stored = {**snap_settings, "utility": utility.model_dump() if utility is not None else None}
    upd = _DetailUpdate(
        identifier=str(t.get("identifier")), unit_key=unit_key, name=name, model_number=model,
        sensors_meta=sp.meta, settings=stored, revision=revision, new_mappings=sp.new_mappings,
    )
    return snap, upd


def _check_hold(hold: HoldInfo | None, heat: float, cool: float, now: datetime, hours: int,
                sent_at: datetime | None = None) -> str | None:
    """None when the running event is the hold this write asked for; otherwise why not.

    ``now`` is the thermostat's own ``utcTime`` from the read-back and ``sent_at`` is when the
    setHold went out, moved onto the thermostat's clock (``_thermostat_clock``), so server clock
    skew cannot fail (or pass) the check. The hold must end no later than ``now + hours``
    (+15 min) and no earlier than ``sent_at + hours`` (-10 min): an OLDER hold with the same
    setpoints and little time left (a renewal that never reached ecobee) is not this write."""
    if hold is None:
        return "no running hold after setHold"
    if P.is_event_hold(hold):
        return f"a running {hold.hold_type} event overrides the hold"
    if hold.kind != "temperature" or hold.heat_f is None or hold.cool_f is None:
        return "the running hold is not a temperature hold"
    if abs(hold.heat_f - heat) > 0.1 or abs(hold.cool_f - cool) > 0.1:
        return f"read-back {hold.heat_f}/{hold.cool_f}°F does not match requested {heat}/{cool}°F"
    if hold.end is None or hold.end > now + timedelta(hours=hours) + HOLD_END_SLACK:
        return "the running hold does not end within the requested hours"
    if sent_at is not None and hold.end < sent_at + timedelta(hours=hours) - RENEWAL_SLACK:
        left = max(0, int((hold.end - now).total_seconds() // 60))
        return (f"the running hold ends in {left} min, not {hours} h after this write: it is an older hold "
                "with the same setpoints, so this write did not land")
    return None


def _running_dr(t: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in t.get("events") or []
            if isinstance(e, dict) and e.get("type") == "demandResponse" and P.parse_bool(e.get("running"))]


def _dr_running(t: dict[str, Any]) -> bool:
    """Any demandResponse event running on this thermostat (reported in before / readback)."""
    return bool(_running_dr(t))


def _dr_identities(t: dict[str, Any], tz: tzinfo | None) -> set[str]:
    """``event_identity`` of every demandResponse event running on this thermostat (the opt-out
    read-back looks for the one it cancelled)."""
    off = P.thermostat_utc_offset(t)
    out: set[str] = set()
    for e in _running_dr(t):
        h = P.parse_hold(e, off, None, None, tz)
        out.add(event_identity(h.link_ref, h.event_name, h.start))
    return out


def _settings_of(t: dict[str, Any]) -> dict[str, object]:
    """Smart Away and Follow Me as the thermostat reports them (WriteResult before / readback)."""
    return {k: (t.get("settings") or {}).get(k) for k in SETTABLE_SETTINGS}


def _settings_match(values: dict[str, object], wanted: dict[str, bool]) -> bool:
    return all(P.parse_bool(values.get(k)) is v for k, v in wanted.items())


def _unit_order(unit_key: str) -> tuple[int, str]:
    keys = list(UNIT_KEYS)
    return (keys.index(unit_key) if unit_key in keys else len(keys), unit_key)


def _thermostat_clock(t: dict[str, Any], server_time: datetime, read_at: datetime) -> datetime:
    """``server_time`` on the thermostat's clock: shifted by (thermostat ``utcTime`` - server
    time when that read-back arrived). Without a ``utcTime`` the server clock is used."""
    t_now = P.parse_utc(t.get("utcTime"))
    return server_time if t_now is None else server_time + (t_now - read_at)


# --- the cloud adapter (worker process only) -------------------------------------------


class EcobeeCloud:
    """ThermostatSource for the ecobee cloud. Owns the access token (memory only)."""

    kind = "ecobee"

    def __init__(
        self,
        client_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str = API_BASE,
        token_url: str = TOKEN_URL,
        readback_delay_s: float = 3.0,
    ) -> None:
        self._client_id = client_id
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, transport=transport,
                                       headers={"Accept": "application/json"})
        self._api_base = api_base.rstrip("/")
        self._token_url = token_url
        self._readback_delay_s = readback_delay_s
        self._access_token: str | None = None
        self._access_expires_at: datetime | None = None
        self._unsaved_refresh: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self._auth_failed: str | None = None
        self._last_success_at: datetime | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._tz_cache: dict[str, tzinfo] = {}
        self._climate_refs: dict[str, dict[str, str]] = {}
        self._closed = False

    @classmethod
    def from_settings(cls) -> EcobeeCloud:
        return cls(get_settings().ecobee_web_client_id)

    # --- bookkeeping -------------------------------------------------------------------

    def _note_success(self) -> None:
        self._consecutive_failures = 0
        self._last_error = None
        self._last_success_at = utcnow()

    def _note_failure(self, detail: str) -> None:
        self._consecutive_failures += 1
        self._last_error = detail
        log.warning("ecobee: %s (failure %d in a row)", detail, self._consecutive_failures)

    async def _auth_fail(self, message: str) -> None:
        self._access_token = None
        self._access_expires_at = None
        changed = self._auth_failed != message
        self._auth_failed = message
        self._note_failure(message)
        if changed:
            try:
                await _db(_merge_status, last_error=message, error_at=utcnow().isoformat())
            except Exception:
                log.exception("could not record the ecobee sign-in failure")

    # --- tokens ------------------------------------------------------------------------

    def _current_refresh_token(self, session: Session) -> str | None:
        if self._unsaved_refresh:
            put_secret(session, REFRESH_SECRET, self._unsaved_refresh)
            return self._unsaved_refresh
        return get_secret(session, REFRESH_SECRET)

    async def _token(self) -> str:
        if self._access_token and self._access_expires_at and utcnow() < self._access_expires_at:
            return self._access_token
        await self._refresh(stale=self._access_token)
        if not self._access_token:
            raise EcobeeAuthError("ecobee access token unavailable")
        return self._access_token

    async def _refresh(self, stale: str | None) -> None:
        """Refresh-token grant against auth.ecobee.com; persists a rotated refresh token."""
        async with self._refresh_lock:
            if self._access_token and self._access_token != stale and self._access_expires_at \
                    and utcnow() < self._access_expires_at:
                return  # another task refreshed while we waited
            try:
                refresh_token = await _db(self._current_refresh_token)
                if self._unsaved_refresh and refresh_token == self._unsaved_refresh:
                    self._unsaved_refresh = None
            except SecretsUnavailable as exc:
                await self._auth_fail(f"ecobee refresh token unreadable: {exc}")
                raise EcobeeAuthError(str(exc)) from exc
            except Exception as exc:
                if not self._unsaved_refresh:
                    self._note_failure("database unavailable for the ecobee token refresh")
                    raise EcobeeApiError("database unavailable for the ecobee token refresh") from exc
                refresh_token = self._unsaved_refresh
            if not refresh_token:
                msg = "ecobee is not signed in; sign in from Setup."
                await self._auth_fail(msg)
                raise EcobeeAuthError(msg)
            try:
                resp = await self._http.post(
                    self._token_url,
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self._client_id},
                )
            except httpx.HTTPError as exc:
                self._note_failure(f"token refresh failed ({type(exc).__name__})")
                raise EcobeeApiError(f"ecobee token refresh failed ({type(exc).__name__})", transport=True) from exc
            payload = _json(resp)
            if resp.status_code in (400, 401, 403):
                err = payload.get("error") if isinstance(payload, dict) else None
                if err == "invalid_grant":
                    msg = "ecobee sign-in expired or was revoked (invalid_grant); sign in again from Setup."
                else:
                    msg = f"ecobee refused the token refresh ({err or resp.status_code}); sign in again from Setup."
                await self._auth_fail(msg)
                raise EcobeeAuthError(msg)
            if resp.status_code >= 300 or not isinstance(payload, dict) or not payload.get("access_token"):
                self._note_failure(f"token refresh failed (HTTP {resp.status_code})")
                raise EcobeeApiError(f"ecobee token refresh failed (HTTP {resp.status_code})",
                                     status_code=resp.status_code)
            new_refresh = payload.get("refresh_token")
            if isinstance(new_refresh, str) and new_refresh and new_refresh != refresh_token:
                self._unsaved_refresh = new_refresh
                try:
                    await _db(put_secret, REFRESH_SECRET, new_refresh)
                    self._unsaved_refresh = None
                except Exception:  # noqa: BLE001 - kept in memory and retried before the next refresh
                    log.error("could not persist the rotated ecobee refresh token; keeping it in memory")
            self._access_token = str(payload["access_token"])
            expires_in = P.num(payload.get("expires_in")) or 3600.0
            self._access_expires_at = utcnow() + timedelta(seconds=max(60.0, expires_in - 120.0))
            if self._auth_failed:
                self._auth_failed = None
                try:
                    await _db(_merge_status, last_error=None, error_at=None)
                except Exception:
                    log.exception("could not clear the ecobee sign-in error")

    # --- HTTP --------------------------------------------------------------------------

    async def _request(self, method: str, endpoint: str, *, params: dict[str, str],
                       body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One API call with the bearer token; on 401 / status 1, 14, 16 refresh once and retry."""
        if self._closed:
            raise EcobeeApiError("ecobee adapter is closed")
        retried = False
        while True:
            token = await self._token()
            try:
                resp = await self._http.request(
                    method, f"{self._api_base}/{endpoint}", params=params, json=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json;charset=UTF-8"},
                )
            except httpx.HTTPError as exc:
                self._note_failure(f"{endpoint} request failed ({type(exc).__name__})")
                raise EcobeeApiError(f"ecobee {endpoint} request failed ({type(exc).__name__})",
                                     transport=True) from exc
            payload = _json(resp)
            code, message = _status_of(payload)
            token_problem = resp.status_code == 401 or code in (1, 14, 16)
            if token_problem and not retried:
                retried = True
                await self._refresh(stale=token)
                continue
            if token_problem:
                msg = f"ecobee rejected a freshly refreshed token (status {code}); sign in again from Setup."
                await self._auth_fail(msg)
                raise EcobeeAuthError(msg)
            if resp.status_code >= 400 or code not in (None, 0) or not isinstance(payload, dict):
                detail = f"ecobee {endpoint}: HTTP {resp.status_code}"
                if code is not None:
                    detail += f", status {code}: {message or 'no message'}"
                self._note_failure(detail)
                raise EcobeeApiError(detail, status_code=resp.status_code, ecobee_code=code)
            self._note_success()
            return payload

    async def _get(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", endpoint, params={"json": _dumps(body)})

    async def _get_thermostats(self, identifiers: list[str], includes: dict[str, bool]) -> list[dict[str, Any]]:
        sel = {"selectionType": "thermostats", "selectionMatch": ",".join(identifiers), **includes}
        payload = await self._get("thermostat", {"selection": sel})
        tstats = [t for t in payload.get("thermostatList") or [] if isinstance(t, dict)]
        for t in tstats:
            ident = str(t.get("identifier"))
            climates = (t.get("program") or {}).get("climates") or []
            if climates:
                self._climate_refs[ident] = {str(c.get("name")): str(c.get("climateRef")) for c in climates
                                             if c.get("name") and c.get("climateRef")}
        return tstats

    async def _get_one(self, identifier: str, includes: dict[str, bool]) -> dict[str, Any]:
        for t in await self._get_thermostats([identifier], includes):
            if str(t.get("identifier")) == identifier:
                return t
        raise EcobeeApiError(f"thermostat {identifier} was not returned by ecobee")

    def _tz_of(self, identifier: str, inv: _Inventory, t: dict[str, Any] | None = None) -> tzinfo | None:
        """The thermostat's IANA zone for event times: ``t``'s location.timeZone when it carries
        one, else the last one seen, else the one Setup's thermostat row stored. None = convert
        with the current offset."""
        tz = _zone(((t or {}).get("location") or {}).get("timeZone"))
        if tz is None:
            tz = self._tz_cache.get(identifier) or _zone(
                (inv.thermostat_settings.get(identifier) or {}).get("timeZone"))
        if tz is not None:
            self._tz_cache[identifier] = tz
        return tz

    async def _post(self, identifier: str, payload: dict[str, Any]) -> None:
        body = {"selection": {"selectionType": "thermostats", "selectionMatch": identifier}, **payload}
        await self._request("POST", "thermostat", params={"format": "json"}, body=body)

    # --- reads -------------------------------------------------------------------------

    async def poll_revisions(self) -> dict[str, str]:
        payload = await self._get("thermostatSummary", {"selection": SUMMARY_SELECTION})
        entries = P.parse_summary(payload)
        unit_by_ident = await _db(_sync_summary, entries)
        return {unit_by_ident[e.identifier]: e.token for e in entries if e.identifier in unit_by_ident}

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        inv = await _db(_load_inventory)
        targets = {i: u for u, i in inv.ident_by_unit.items() if unit_keys is None or u in unit_keys}
        if not targets:
            return []
        tstats = await self._get_thermostats(sorted(targets), DETAIL_INCLUDES)
        now = utcnow()
        snaps: list[UnitSnapshot] = []
        updates: list[_DetailUpdate] = []
        for t in tstats:
            ident = str(t.get("identifier"))
            unit = targets.get(ident)
            if unit is None:
                continue
            snap, upd = _snapshot_from(unit, t, inv.sensors, inv.our_holds.get(unit), now)
            tz = _zone((t.get("location") or {}).get("timeZone"))
            if tz is not None:
                self._tz_cache[ident] = tz
            snaps.append(snap)
            updates.append(upd)
        missing = set(targets) - {u.identifier for u in updates}
        if missing:
            log.warning("ecobee did not return mapped thermostat(s) %s", ", ".join(sorted(missing)))
        if updates:
            await _db(_persist_details, updates)
        order = {k: i for i, k in enumerate(UNIT_KEYS)}
        return sorted(snaps, key=lambda s: order.get(s.unit_key, 99))

    async def _time_zones(self, identifiers: list[str], inv: _Inventory) -> dict[str, tzinfo]:
        """Each thermostat's IANA zone (location.timeZone), for runtimeReport rows. Falls back to
        the house's configured zone when ecobee does not report a valid one."""
        out: dict[str, tzinfo] = {}
        missing: list[str] = []
        for ident in identifiers:
            tz = self._tz_cache.get(ident) or _zone((inv.thermostat_settings.get(ident) or {}).get("timeZone"))
            if tz is not None:
                self._tz_cache[ident] = tz
                out[ident] = tz
            else:
                missing.append(ident)
        if missing:
            try:
                for t in await self._get_thermostats(missing, {"includeLocation": True}):
                    ident = str(t.get("identifier"))
                    name = (t.get("location") or {}).get("timeZone")
                    tz = _zone(name)
                    if tz is not None and ident in missing:
                        self._tz_cache[ident] = out[ident] = tz
                        await _db(_set_tz, ident, str(name))
            except EcobeeApiError:
                log.warning("could not read thermostat time zones; using the house time zone")
            fallback = _zone(inv.house_tz) or UTC
            for ident in missing:
                if ident not in out:
                    log.warning("thermostat %s reports no usable time zone; using %s", ident, inv.house_tz)
                    self._tz_cache[ident] = out[ident] = fallback
        return out

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        if end <= start:
            return []
        inv = await _db(_load_inventory)
        identifiers = sorted(inv.unit_by_ident)
        if not identifiers:
            return []
        tzs = await self._time_zones(identifiers, inv)
        out: list[RuntimeInterval] = []
        for first, last in P.report_chunks(start, end):
            body = P.report_request(identifiers, first, last)
            async with self._report_lock:  # ecobee: one runtimeReport request at a time
                payload = await self._request("GET", "runtimeReport", params={"format": "json", "body": _dumps(body)})
            out.extend(P.parse_runtime_report(payload, inv.unit_by_ident, inv.sensors, tzs, start, end,
                                              self._climate_refs))
        return out

    # --- writes ------------------------------------------------------------------------

    async def set_hold(self, req: HoldRequest) -> WriteResult:
        """setHold (holdHours 1-2, tenths) decided on a FRESH GET, then read back up to twice.

        Refused before anything is sent (``base.write_refusal``, ``request['refused']``): a
        controller write (``req.by_owner`` False) over a plain hold that is not ours, or a
        Quick Save (``request['not_ours']``: someone set it since the snapshot the controller
        decided on), and any write over a running vacation, demand-response or unknown event
        (``request['event']``). Smart Away / Home and our own hold never refuse; the owner's
        hold skips the person check. Then the thermostat's heatCoolMinDelta is checked.

        A verified hold is recorded in ``ecobee_holds`` (the owner's marked ``by_owner``: it is
        a person's hold, never ours). A controller write that went out but did not verify (a
        failed read-back GET, a lagging or mismatched read-back) is noted as that record's
        ``attempt``, so if it landed a later fresh read still sees it as ours rather than
        refusing the controller's next write over its own hold."""
        from climate.control.guardrails import round_setpoint

        by_owner = bool(req.by_owner)
        request: dict[str, object] = {"unit_key": req.unit_key, "heat_f": req.heat_f, "cool_f": req.cool_f,
                                      "hours": req.hours, "reason": req.reason, "by_owner": by_owner}
        before: dict[str, object] = {}
        try:
            inv = await _db(_load_inventory)
            ident = inv.ident_by_unit.get(req.unit_key)
            if not ident:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"unit {req.unit_key!r} is not mapped to an ecobee thermostat")
            hours = int(req.hours)
            if hours < 1 or hours > 2:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"holdHours must be 1 or 2 (got {req.hours})")
            heat = round_setpoint(req.heat_f)
            cool = round_setpoint(req.cool_f)
            params = {"holdType": "holdHours", "holdHours": hours,
                      "heatHoldTemp": round(heat * 10), "coolHoldTemp": round(cool * 10)}
            request.update(identifier=ident, heat_f=heat, cool_f=cool,
                           function={"type": "setHold", "params": params})
            ours = inv.our_holds.get(req.unit_key)
            tz = self._tz_of(ident, inv)
            t_before = await self._get_one(ident, READBACK_INCLUDES)
            read0_at = utcnow()
            before = _write_state(t_before, ours, tz)
            refusal = write_refusal(_hold_of(t_before, ours, tz), by_owner=by_owner)
            if refusal is not None:
                extra, error = refusal
                return WriteResult(ok=False, channel="ecobee", before=before, request={**request, **extra},
                                   error=error)
            min_delta = P.tenths_to_f((t_before.get("settings") or {}).get("heatCoolMinDelta")) or 0.0
            if cool - heat < min_delta or cool <= heat:
                return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                   error=f"cool - heat = {cool - heat:.1f}°F is below the thermostat's "
                                         f"heatCoolMinDelta {min_delta:.1f}°F; not sent")
            sent_at = utcnow()
            post_error: str | None = None
            try:
                await self._post(ident, {"functions": [{"type": "setHold", "params": params}]})
            except EcobeeApiError as exc:
                if not exc.transport:
                    return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))
                post_error = str(exc)  # may have landed: the read-back decides
            request["sent"] = True
            readback: dict[str, object] | None = None
            why: str | None = "no read-back"
            # sent_at on the thermostat's clock (from the read before the write; a read-back refines it)
            sent_tc = _thermostat_clock(t_before, sent_at, read0_at)
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(self._readback_delay_s)
                try:
                    t_after = await self._get_one(ident, READBACK_INCLUDES)
                except EcobeeApiError as exc:
                    if readback is None:
                        why = f"setHold sent; read-back failed ({exc})"
                    continue
                read_at = utcnow()
                sent_tc = _thermostat_clock(t_after, sent_at, read_at)
                hold = _hold_of(t_after, None, tz)
                readback = _write_state(t_after, None, tz)
                why = _check_hold(hold, heat, cool, _thermostat_clock(t_after, read_at, read_at), hours,
                                  sent_at=sent_tc)
                if why is None and hold is not None:
                    record: dict[str, Any] = {"heat_f": heat, "cool_f": cool,
                                              "end": hold.end.isoformat() if hold.end else None,
                                              "hours": hours, "written_at": sent_at.isoformat()}
                    if by_owner:
                        record["by_owner"] = True
                    await _db(_record_hold, req.unit_key, record)
                    readback = _write_state(t_after, record, tz)  # ours (set_by_us) unless the owner's
                    break
            if why is not None:
                if not by_owner:
                    # the window _check_hold accepts: if this write landed, its hold ends in it
                    window = {"heat_f": heat, "cool_f": cool, "sent_at": sent_at.isoformat(),
                              "end_from": (sent_tc + timedelta(hours=hours) - RENEWAL_SLACK).isoformat(),
                              "end_to": (sent_tc + timedelta(hours=hours) + HOLD_END_SLACK).isoformat()}
                    await _db(_record_attempt, req.unit_key, window)
                if post_error:
                    why = f"{post_error}; {why}"
            return WriteResult(ok=why is None, channel="ecobee", before=before, request=request,
                               readback=readback, error=why)
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    async def resume_program(self, unit_key: str, reason: str, force: bool = False) -> WriteResult:
        """resumeProgram (resumeAll=false) on a running plain HOLD, then read back.

        Decided on a fresh GET. Without ``force`` only the controller's own hold (it matches the
        ``ecobee_holds`` record: ``set_by_us``) is cancelled; a hand-set hold, or the owner's
        from the app, is refused (``NOT_OURS_ERROR``), and so is every event. ``force`` (the
        owner's explicit resume: Back to automatic / Resume schedule) cancels any plain hold,
        a Quick Save (``PERSON_EVENTS``, which this app shows as a person's hold) and Smart Away
        / Home (``AUTO_EVENTS``): ecobee's resumeProgram removes the top running event. Vacation,
        demand response and types this app does not know are never cancelled from here, forced
        or not: opting out of a utility event is ``opt_out_event``'s job. ok only when the
        read-back shows no plain hold, and not the Quick Save / Smart Away it cancelled, on
        top."""
        request: dict[str, object] = {"unit_key": unit_key, "reason": reason, "force": force}
        before: dict[str, object] = {}
        try:
            inv = await _db(_load_inventory)
            ident = inv.ident_by_unit.get(unit_key)
            if not ident:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"unit {unit_key!r} is not mapped to an ecobee thermostat")
            ours = inv.our_holds.get(unit_key)
            tz = self._tz_of(ident, inv)
            t_before = await self._get_one(ident, READBACK_INCLUDES)
            before = _write_state(t_before, ours, tz)
            current = _hold_of(t_before, ours, tz)
            if current is None:
                await _db(_record_hold, unit_key, None)
                return WriteResult(ok=True, channel="ecobee", before=before, request={**request, "noop": True},
                                   readback=before)
            if P.is_event_hold(current):
                if not (force and current.hold_type in (*PERSON_EVENTS, *AUTO_EVENTS)):
                    return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                       error=f"a running {current.hold_type} event is in effect; not resuming")
            elif not force and not current.set_by_us:
                return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                   error=NOT_OURS_ERROR)
            # what must be gone afterwards: any plain hold, and the person / auto event cancelled
            gone = (*P.PLAIN_HOLD_TYPES, str(current.hold_type))
            function = {"type": "resumeProgram", "params": {"resumeAll": False}}
            request.update(identifier=ident, function=function)
            await self._post(ident, {"functions": [function]})
            request["sent"] = True
            try:
                t_after = await self._get_one(ident, READBACK_INCLUDES)
            except EcobeeApiError as exc:
                return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                   error=f"resumeProgram sent; read-back failed ({exc})")
            readback = _write_state(t_after, ours, tz)
            after = _hold_of(t_after, ours, tz)
            if after is None or after.hold_type not in gone:
                await _db(_record_hold, unit_key, None)
                return WriteResult(ok=True, channel="ecobee", before=before, request=request, readback=readback)
            still = ("a hold is" if after.hold_type in P.PLAIN_HOLD_TYPES
                     else f"the {after.hold_type} event is")
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, readback=readback,
                               error=f"{still} still running after resumeProgram")
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    async def opt_out_event(self, unit_key: str, reason: str, *, link_ref: str | None = None,
                            name: str | None = None, start: datetime | None = None) -> WriteResult:
        """Opt out of the RUNNING utility (demand-response) event: ``resumeProgram``
        (resumeAll=false) with that event on top, ecobee's documented cancel for an event that
        is not mandatory, which ecobee records and reports to the utility.

        Decided on a fresh GET (with location.timeZone, so event times convert as the snapshot
        that listed the event did). Refused before anything is sent (``request['refused']``)
        when the top running event is not demandResponse (a plain hold or another event on top
        is never touched); when an identity is given (``link_ref`` / ``name`` + ``start``: the
        event the skip was asked for) and the top event is a different one
        (``request['other_event']``; the asked event may have ended and the next one started
        since the snapshot the skip was decided on); when the event is mandatory
        (``isOptional`` false; ``request['mandatory']`` True); or when it ends within
        ``OPT_OUT_END_GUARD`` (the resumeProgram could land after it ended and cancel whatever
        runs underneath).

        Then reads back, up to twice ``readback_delay_s`` apart, that THIS event (by
        ``event_identity``) no longer runs: ok only then; another utility event still running
        does not fail it. A read-back GET that fails moves on to the second one; when neither
        succeeds the error says "resumeProgram sent; read-back failed". ``request['sent']`` is
        True once the POST went out. A POST that fails in transit may still have landed, so
        the read-back decides, as for holds. Whatever runs underneath (a plain hold, the
        schedule) is left as ecobee resumes it."""
        request: dict[str, object] = {"unit_key": unit_key, "reason": reason}
        asked = (None if link_ref is None and name is None and start is None
                 else event_identity(link_ref, name, start))
        before: dict[str, object] = {}
        try:
            inv = await _db(_load_inventory)
            ident = inv.ident_by_unit.get(unit_key)
            if not ident:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"unit {unit_key!r} is not mapped to an ecobee thermostat")
            request["identifier"] = ident
            ours = inv.our_holds.get(unit_key)
            t_before = await self._get_one(ident, OPT_OUT_INCLUDES)
            tz = self._tz_of(ident, inv, t_before)
            before = {**_write_state(t_before, ours, tz), "demand_response_running": _dr_running(t_before)}
            current = _hold_of(t_before, ours, tz)
            if current is None or current.hold_type != "demandResponse":
                on_top = ("nothing is running" if current is None
                          else f"a {current.hold_type} event is on top" if P.is_event_hold(current)
                          else "a hold is on top")
                return WriteResult(ok=False, channel="ecobee", before=before,
                                   request={**request, "refused": True, "mandatory": False},
                                   error=f"no utility event to opt out of ({on_top}); nothing sent")
            label = f" {current.event_name!r}" if current.event_name else ""
            request.update(event_name=current.event_name, link_ref=current.link_ref)
            top = event_identity(current.link_ref, current.event_name, current.start)
            if asked is not None and top != asked:
                return WriteResult(ok=False, channel="ecobee", before=before,
                                   request={**request, "asked_for": asked, "refused": True, "mandatory": False,
                                            "other_event": True},
                                   error="a different utility event is on top; nothing sent")
            if current.is_optional is False:
                return WriteResult(ok=False, channel="ecobee", before=before,
                                   request={**request, "refused": True, "mandatory": True},
                                   error=f"the utility event{label} is mandatory: ecobee does not allow "
                                         f"opting out of it; nothing sent")
            t_now = P.parse_utc(t_before.get("utcTime")) or utcnow()
            if current.end is not None and current.end - t_now <= OPT_OUT_END_GUARD:
                guard_min = int(OPT_OUT_END_GUARD.total_seconds() // 60)
                return WriteResult(ok=False, channel="ecobee", before=before,
                                   request={**request, "refused": True, "mandatory": False},
                                   error=f"the utility event{label} ends within {guard_min} minutes; nothing "
                                         f"sent (a late resumeProgram could cancel what runs underneath)")
            function = {"type": "resumeProgram", "params": {"resumeAll": False}}
            request["function"] = function
            post_error: str | None = None
            try:
                await self._post(ident, {"functions": [function]})
            except EcobeeApiError as exc:
                if not exc.transport:
                    return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                       error=str(exc))
                post_error = str(exc)  # may have landed: the read-back decides
            request["sent"] = True
            readback: dict[str, object] | None = None
            why: str | None = "no read-back"
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(self._readback_delay_s)
                try:
                    t_after = await self._get_one(ident, READBACK_INCLUDES)
                except EcobeeApiError as exc:
                    if readback is None:  # a read-back that did arrive keeps its verdict
                        why = f"resumeProgram sent; read-back failed ({exc})"
                    continue
                running = top in _dr_identities(t_after, tz)
                readback = {**_write_state(t_after, ours, tz), "demand_response_running": _dr_running(t_after),
                            "event_running": running}
                why = f"the utility event{label} is still running after resumeProgram" if running else None
                if why is None:
                    break
            if why is not None and post_error:
                why = f"{post_error}; {why}"
            return WriteResult(ok=why is None, channel="ecobee", before=before, request=request,
                               readback=readback, error=why)
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    async def apply_settings(self, unit_key: str, settings: dict[str, bool], reason: str) -> WriteResult:
        """Write Smart Away (``autoAway``) and/or Follow Me (``followMeComfort``) on ONE
        thermostat and read them back (used by "Hand back to ecobee"). Same updateThermostat
        shape as ``ensure_settings``; any other key or a non-boolean value is refused before
        anything is sent. ``request`` {unit_key, identifier, settings, reason} (+ ``noop``
        when the thermostat already reads the asked values), ``before`` / ``readback`` both
        settings as read. A POST that fails in transit may still have landed: the read-back
        decides."""
        wanted = dict(settings)
        request: dict[str, object] = {"unit_key": unit_key, "settings": wanted, "reason": reason}
        bad = sorted(k for k in wanted if k not in SETTABLE_SETTINGS)
        if not wanted or bad or any(not isinstance(v, bool) for v in wanted.values()):
            what = f"unsupported setting(s) {', '.join(bad)}" if bad else "no settings" if not wanted else \
                "values must be true or false"
            return WriteResult(ok=False, channel="ecobee", request=request,
                               error=f"{what}; only {' and '.join(SETTABLE_SETTINGS)} can be written; "
                                     f"nothing sent")
        before: dict[str, object] = {}
        try:
            inv = await _db(_load_inventory)
            ident = inv.ident_by_unit.get(unit_key)
            if not ident:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"unit {unit_key!r} is not mapped to an ecobee thermostat")
            request["identifier"] = ident
            before = _settings_of(await self._get_one(ident, {"includeSettings": True}))
            if _settings_match(before, wanted):
                return WriteResult(ok=True, channel="ecobee", before=before,
                                   request={**request, "noop": True}, readback=dict(before))
            post_error: str | None = None
            try:
                await self._post(ident, {"thermostat": {"settings": wanted}})
            except EcobeeApiError as exc:
                if not exc.transport:
                    return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                       error=str(exc))
                post_error = str(exc)  # may have landed: the read-back decides
            readback = _settings_of(await self._get_one(ident, {"includeSettings": True}))
            ok = _settings_match(readback, wanted)
            error = None if ok else "settings did not read back as " + ", ".join(
                f"{k}={str(v).lower()}" for k, v in wanted.items())
            if error and post_error:
                error = f"{post_error}; {error}"
            return WriteResult(ok=ok, channel="ecobee", before=before, request=request, readback=readback,
                               error=error)
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    async def ensure_settings(self, reason: str = ENSURE_SETTINGS_REASON) -> list[WriteResult]:
        """autoAway=false and followMeComfort=false on every mapped unit (checked daily).

        One WriteResult per unit, ready to log: ``request`` {unit_key, identifier, settings,
        reason} (+ ``noop`` when nothing was sent), ``before`` the two settings as read,
        ``readback`` the two settings as read back after the write (for a no-op, the read
        itself). Idempotent: a unit whose settings already read false gets no write at all.
        ``before`` / ``readback`` stay empty only when ecobee could not be read."""
        inv = await _db(_load_inventory)
        if not inv.ident_by_unit:
            return []

        def request_for(unit: str, ident: str) -> dict[str, object]:
            return {"unit_key": unit, "identifier": ident, "settings": dict(WANTED_SETTINGS), "reason": reason}

        def all_off(values: dict[str, object]) -> bool:
            return _settings_match(values, WANTED_SETTINGS)

        results: dict[str, WriteResult] = {}
        try:
            tstats = {str(t.get("identifier")): t
                      for t in await self._get_thermostats(sorted(inv.unit_by_ident), {"includeSettings": True})}
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return [WriteResult(ok=False, channel="ecobee", request=request_for(u, i), error=f"read failed: {exc}")
                    for u, i in sorted(inv.ident_by_unit.items(), key=lambda kv: _unit_order(kv[0]))]
        to_verify: list[str] = []
        for unit, ident in inv.ident_by_unit.items():
            request = request_for(unit, ident)
            t = tstats.get(ident)
            if t is None:
                results[unit] = WriteResult(ok=False, channel="ecobee", request=request,
                                            error="thermostat not returned by ecobee; not written")
                continue
            current = _settings_of(t)
            if all_off(current):
                results[unit] = WriteResult(ok=True, channel="ecobee", before=current,
                                            request={**request, "noop": True}, readback=dict(current))
                continue
            try:
                await self._post(ident, {"thermostat": {"settings": dict(WANTED_SETTINGS)}})
                to_verify.append(ident)
                results[unit] = WriteResult(ok=False, channel="ecobee", before=current, request=request,
                                            error="not verified")
            except (EcobeeApiError, EcobeeAuthError) as exc:
                results[unit] = WriteResult(ok=False, channel="ecobee", before=current, request=request,
                                            error=str(exc))
        if to_verify:
            try:
                after = {str(t.get("identifier")): t for t in await self._get_thermostats(to_verify, {"includeSettings": True})}
            except (EcobeeApiError, EcobeeAuthError) as exc:
                after = {}
                for ident in to_verify:
                    results[inv.unit_by_ident[ident]].error = f"sent, but the read-back failed: {exc}"
            for ident in to_verify:
                res = results[inv.unit_by_ident[ident]]
                t = after.get(ident)
                if t is None:
                    if after:
                        res.error = "sent, but the thermostat was not returned in the read-back"
                    continue
                readback = _settings_of(t)
                res.readback = readback
                res.ok = all_off(readback)
                res.error = None if res.ok else "settings did not read back as autoAway=false, followMeComfort=false"
        return [results[u] for u in sorted(results, key=_unit_order)]

    async def update_sensor_sets(self, unit_key: str, sets: dict[str, list[str]], reason: str) -> WriteResult:
        """Read-modify-write the program's comfort-setting sensor participation (fresh GET +
        revision check). Keep Home's set current: temperature holds use Home's sensors; that
        is the caller's job (only the climates in ``sets`` are written).

        Always a controller (or hand-back) write: when it would change anything, it is refused
        before anything is sent under the same rules as the controller's ``set_hold``
        (``base.write_refusal`` on the fresh GET's running top override, shown in
        ``before['hold']``): a person's hold or Quick Save (``request['not_ours']``), or a
        vacation, demand-response or unknown event (``request['event']``). A write that would
        change nothing is a no-op whatever runs."""
        request: dict[str, object] = {"unit_key": unit_key, "sets": {k: list(v) for k, v in sets.items()},
                                      "reason": reason}
        before: dict[str, object] = {}
        try:
            inv = await _db(_load_inventory)
            ident = inv.ident_by_unit.get(unit_key)
            if not ident:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"unit {unit_key!r} is not mapped to an ecobee thermostat")
            request["identifier"] = ident
            t = await self._get_one(ident, PROGRAM_INCLUDES)
            current = _hold_of(t, inv.our_holds.get(unit_key), self._tz_of(ident, inv))
            rev0 = str(t.get("thermostatRev") or "")
            program = copy.deepcopy(t.get("program") or {})
            remote = t.get("remoteSensors") or []
            sp = P.parse_remote_sensors(unit_key, remote, inv.sensors, utcnow())
            names = {str(r.get("id")): str(r.get("name") or "") for r in remote}
            key_to_id = {k: i for i, k in sp.id_to_key.items()}
            before = {"revision": rev0, "sets": P.parse_sensor_sets(program, sp.id_to_key),
                      "hold": current.model_dump(mode="json") if current is not None else None}
            climates = {str(c.get("climateRef")): c for c in program.get("climates") or [] if c.get("climateRef")}
            problems: list[str] = []
            for ref, keys in sets.items():
                if ref not in climates:
                    problems.append(f"comfort setting {ref!r} does not exist on this thermostat")
                if not keys:
                    problems.append(f"comfort setting {ref!r} needs at least one sensor")
                for key in keys:
                    if key not in key_to_id:
                        problems.append(f"sensor {key!r} is not a mapped sensor of this thermostat")
            if problems:
                return WriteResult(ok=False, channel="ecobee", before=before, request=request, error="; ".join(problems))
            expected = {ref: sorted({key_to_id[k] for k in keys}) for ref, keys in sets.items()}
            current_ids = P.climate_sensor_ids(program)
            if all(sorted(set(current_ids.get(ref, []))) == ids for ref, ids in expected.items()):
                if sp.new_mappings:
                    await _db(_save_mappings, unit_key, sp.new_mappings)
                return WriteResult(ok=True, channel="ecobee", before=before, request={**request, "noop": True},
                                   readback=before)
            refusal = write_refusal(current, by_owner=False)
            if refusal is not None:
                extra, error = refusal
                return WriteResult(ok=False, channel="ecobee", before=before, request={**request, **extra},
                                   error=error)
            for ref, keys in sets.items():
                ordered: list[str] = []
                for k in keys:
                    if key_to_id[k] not in ordered:
                        ordered.append(key_to_id[k])
                climates[ref]["sensors"] = [{"id": f"{sid}:1", "name": names.get(sid, "")} for sid in ordered]
            program.pop("currentClimateRef", None)
            summary = await self._get("thermostatSummary", {"selection": {
                "selectionType": "thermostats", "selectionMatch": ident, "includeEquipmentStatus": False}})
            now_rev = next((e.thermostat_rev for e in P.parse_summary(summary) if e.identifier == ident), None)
            if now_rev != rev0:
                return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                   error=f"thermostat revision moved ({rev0} -> {now_rev}) since the program was "
                                         f"read; not written")
            request["program_climates"] = {ref: climates[ref]["sensors"] for ref in sets}
            await self._post(ident, {"thermostat": {"program": program}})
            t_after = await self._get_one(ident, PROGRAM_INCLUDES)
            after_ids = P.climate_sensor_ids(t_after.get("program") or {})
            readback = {"revision": str(t_after.get("thermostatRev") or ""),
                        "sets": P.parse_sensor_sets(t_after.get("program") or {}, sp.id_to_key)}
            mismatched = [ref for ref, ids in expected.items() if sorted(set(after_ids.get(ref, []))) != ids]
            if sp.new_mappings:
                await _db(_save_mappings, unit_key, sp.new_mappings)
            if mismatched:
                return WriteResult(ok=False, channel="ecobee", before=before, request=request, readback=readback,
                                   error=f"sensor set read-back differs for {', '.join(sorted(mismatched))}")
            return WriteResult(ok=True, channel="ecobee", before=before, request=request, readback=readback)
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    # --- status ------------------------------------------------------------------------

    async def health(self) -> SourceHealth:
        try:
            have_token = await _db(lambda s: get_secret(s, REFRESH_SECRET) is not None) or bool(self._unsaved_refresh)
            db_note = ""
        except SecretsUnavailable as exc:
            have_token, db_note = False, str(exc)
        except Exception:  # noqa: BLE001 - health must answer even when the database is down
            have_token, db_note = bool(self._access_token or self._unsaved_refresh), "database unavailable"
        signed_in = have_token and self._auth_failed is None
        ok = signed_in and self._consecutive_failures < 3
        if self._auth_failed:
            detail = self._auth_failed
        elif not have_token:
            detail = db_note or "ecobee is not signed in"
        elif self._last_error:
            detail = self._last_error
        elif self._last_success_at is None:
            detail = "signed in; no ecobee call yet"
        else:
            detail = "ok"
        return SourceHealth(ok=ok, kind="ecobee", detail=detail, signed_in=signed_in,
                            last_success_at=self._last_success_at, consecutive_failures=self._consecutive_failures)

    async def close(self) -> None:
        self._closed = True
        self._access_token = None
        await self._http.aclose()


# --- sign-in (runs in the API process; holds the MFA challenge in memory) -------------

_MFA: dict[str, Any] = {}  # {'client': Ecobee (password wiped), 'challenge', 'mfa_type', 'expires_at'}
_MFA_LOCK = threading.Lock()


def _patch_client_id() -> str:
    """Point pyecobee at the configured web client id. Its methods read the name
    ``ECOBEE_WEB_CLIENT_ID`` from the ``pyecobee`` module globals at call time (it is imported
    there from ``pyecobee.const``), so assigning the package attribute takes effect."""
    client_id = get_settings().ecobee_web_client_id
    pyecobee.ECOBEE_WEB_CLIENT_ID = client_id  # type: ignore[misc]
    pyecobee.const.ECOBEE_WEB_CLIENT_ID = client_id  # type: ignore[misc]
    return client_id


def _wipe_credentials(client: Any) -> None:
    """Drop the password (and the library's config copy of it, which ``_write_config`` keeps)."""
    client.password = None
    client.config = None


def _wipe_all(client: Any) -> None:
    _wipe_credentials(client)
    client.username = None
    client.access_token = None
    client.refresh_token = None


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def _secrets_problem() -> str | None:
    key = get_settings().secret_key
    if not key:
        return "CLIMATE_SECRET_KEY is not set, so the ecobee sign-in cannot be stored. Set it and restart."
    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        return "CLIMATE_SECRET_KEY is not a valid Fernet key, so the ecobee sign-in cannot be stored."
    return None


def _login_error_message(exc: BaseException, *, mfa: bool = False) -> str:
    if isinstance(exc, EcobeeAuthFailedError):
        return ("ecobee did not accept that code. Check it and try again." if mfa
                else "ecobee did not accept that email and password.")
    text = str(exc)
    if isinstance(exc, EcobeeAuthUnknownError) and ("/u/mfa-" in text or "MFA type" in text):
        return _UNSUPPORTED_MFA
    if isinstance(exc, EcobeeAuthUnknownError):
        return ("ecobee sign-in failed: the sign-in service could not be reached or its pages changed. "
                "Try again later; if it keeps failing, the web client id (CLIMATE_ECOBEE_WEB_CLIENT_ID) "
                "may need updating.")
    return "ecobee sign-in failed unexpectedly. Try again."


def _store_login(session: Session, refresh_token: str) -> None:
    put_secret(session, REFRESH_SECRET, refresh_token)
    put_setting(session, STATUS_KEY, {"signed_in_at": utcnow().isoformat(), "last_error": None}, updated_by="owner")


async def _finish_login(client: Any) -> dict[str, Any]:
    refresh_token = client.refresh_token
    _wipe_all(client)
    if not refresh_token:
        return _error("ecobee signed in but returned no refresh token; try again.")
    try:
        await _db(_store_login, refresh_token)
    except SecretsUnavailable as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - no traceback: SQL errors can echo bound parameters
        log.error("could not store the ecobee sign-in (%s)", type(exc).__name__)
        return _error("Signed in to ecobee, but the sign-in could not be saved. Try again.")
    log.info("ecobee account signed in; refresh token stored")
    return {"status": "signed_in"}


async def start_login(email: str, password: str) -> dict:
    """Return {'status': 'signed_in'} after storing the refresh token, or
    {'status': 'mfa_required', 'mfa_type': 'otp'|'sms'} keeping the challenge in memory for
    10 minutes, or {'status': 'error', 'error': '...'}. The password is never stored."""
    _quiet_library_logger()
    email = (email or "").strip()
    if not email or not password:
        return _error("Enter the ecobee account email and password.")
    problem = _secrets_problem()
    if problem:
        return _error(problem)
    with _MFA_LOCK:
        old = _MFA.get("client")
        if old is not None:
            _wipe_all(old)
        _MFA.clear()
    _patch_client_id()
    client = pyecobee.Ecobee(config={"USERNAME": email, "PASSWORD": password})
    del password
    try:
        await asyncio.to_thread(client.request_tokens_web)
    except EcobeeAuthMfaRequiredError as exc:
        _wipe_credentials(client)
        challenge = exc.challenge
        mfa_type = str(getattr(challenge, "mfa_type", "") or "")
        if mfa_type not in ("otp", "sms"):
            _wipe_all(client)
            return _error(_UNSUPPORTED_MFA)
        with _MFA_LOCK:
            _MFA.update(client=client, challenge=challenge, mfa_type=mfa_type, expires_at=utcnow() + MFA_TTL)
        log.info("ecobee sign-in needs a %s code", mfa_type)
        return {"status": "mfa_required", "mfa_type": mfa_type}
    except Exception as exc:  # noqa: BLE001 - every failure becomes a short, safe message
        _wipe_all(client)
        log.warning("ecobee sign-in failed (%s)", type(exc).__name__)
        return _error(_login_error_message(exc))
    return await _finish_login(client)


async def submit_mfa(code: str) -> dict:
    """Finish a sign-in with the TOTP/SMS code. A wrong code keeps the challenge (until it
    expires) so the owner can retry."""
    code = (code or "").strip()
    with _MFA_LOCK:
        pending = dict(_MFA)
        _MFA.clear()
    if not pending:
        return _error("No ecobee sign-in is waiting for a code. Sign in again.")
    client = pending["client"]
    if pending["expires_at"] <= utcnow():
        _wipe_all(client)
        return _error("The verification code request expired after 10 minutes. Sign in again.")
    if not code.isdigit():
        with _MFA_LOCK:
            if not _MFA:
                _MFA.update(pending)
        return _error("Enter the numeric code from your authenticator app or text message.")
    _patch_client_id()
    try:
        await asyncio.to_thread(client.submit_mfa_code, pending["challenge"], code)
    except EcobeeAuthFailedError as exc:
        with _MFA_LOCK:
            if not _MFA and pending["expires_at"] > utcnow():
                _MFA.update(pending)
        return _error(_login_error_message(exc, mfa=True))
    except Exception as exc:  # noqa: BLE001
        _wipe_all(client)
        log.warning("ecobee MFA step failed (%s)", type(exc).__name__)
        return _error(_login_error_message(exc, mfa=True))
    return await _finish_login(client)


def mfa_pending() -> tuple[bool, str | None]:
    with _MFA_LOCK:
        if not _MFA:
            return False, None
        if _MFA["expires_at"] <= utcnow():
            _wipe_all(_MFA["client"])
            _MFA.clear()
            return False, None
        return True, str(_MFA["mfa_type"])


def _sign_out_db(session: Session) -> None:
    delete_secret(session, REFRESH_SECRET)
    session.execute(delete(AppSetting).where(AppSetting.key == STATUS_KEY))


async def sign_out() -> None:
    """Forget the ecobee sign-in: the stored refresh token, the status and any pending MFA."""
    with _MFA_LOCK:
        client = _MFA.get("client")
        if client is not None:
            _wipe_all(client)
        _MFA.clear()
    await _db(_sign_out_db)
    log.info("ecobee account signed out")

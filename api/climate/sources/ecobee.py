"""The ONE ecobee cloud adapter (CLAUDE.md). Everything ecobee-auth lives behind this file.

Sign-in uses python-ecobee-api 0.4.x account sign-in (Auth0 PKCE web flow, TOTP/SMS MFA)
only for login; token refresh is a direct refresh-token grant against auth.ecobee.com, and every
data call is made here with the bearer token: /1/thermostatSummary (every >= 3 min),
/1/thermostat (on revision change), /1/runtimeReport (<= 31 days per call, one at a time), and
setHold/resumeProgram/settings/program writes, each read back (the library swallows HTTP
errors, so it is never used for data or writes). Rules:
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
    HoldInfo,
    HoldRequest,
    RuntimeInterval,
    SourceHealth,
    UnitSnapshot,
    WriteResult,
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
HOLDS_KEY = "ecobee_holds"  # {unit_key: {heat_f, cool_f, end, hours, written_at}} last hold we wrote
MFA_TTL = timedelta(minutes=10)
HTTP_TIMEOUT_S = 30.0
DETAIL_CACHE_S = 300.0  # a write's "before" may come from a detail fetched this recently
HOLD_END_SLACK = timedelta(minutes=15)

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
}
READBACK_INCLUDES: dict[str, bool] = {
    "includeRuntime": True,
    "includeEvents": True,
    "includeSettings": True,
    "includeProgram": True,
}
PROGRAM_INCLUDES: dict[str, bool] = {"includeProgram": True, "includeSensors": True}

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


# --- building our types from a thermostat object ---------------------------------------


def _hold_of(t: dict[str, Any], ours: dict[str, Any] | None) -> HoldInfo | None:
    ev = P.running_override(t.get("events"))
    if ev is None:
        return None
    return P.parse_hold(ev, P.thermostat_utc_offset(t), t.get("program") or {}, ours)


def _write_state(t: dict[str, Any], ours: dict[str, Any] | None) -> dict[str, object]:
    """Compact state for WriteResult.before / readback."""
    runtime = t.get("runtime") or {}
    settings = t.get("settings") or {}
    program = t.get("program") or {}
    hold = _hold_of(t, ours)
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
    sp = P.parse_remote_sensors(unit_key, t.get("remoteSensors") or [], sensors, ts)
    hold = _hold_of(t, ours)
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
    tz_name = (t.get("location") or {}).get("timeZone")
    if tz_name:
        snap_settings["timeZone"] = tz_name
    if offset is not None:
        snap_settings["utcOffsetMinutes"] = int(offset.total_seconds() // 60)
    revision = P.revision_token(t.get("thermostatRev"), runtime.get("runtimeRev"))
    model = t.get("modelNumber") or None
    name = str(t.get("name") or t.get("identifier"))
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
    )
    upd = _DetailUpdate(
        identifier=str(t.get("identifier")), unit_key=unit_key, name=name, model_number=model,
        sensors_meta=sp.meta, settings=dict(snap_settings), revision=revision, new_mappings=sp.new_mappings,
    )
    return snap, upd


def _check_hold(hold: HoldInfo | None, heat: float, cool: float, now: datetime, hours: int) -> str | None:
    """None when the running event is our hold; otherwise why not. ``now`` is the thermostat's
    own ``utcTime`` from the read-back, so server clock skew cannot fail (or pass) the check."""
    if hold is None:
        return "no running hold after setHold"
    if hold.hold_type in ("vacation", "autoAway", "autoHome", "quickSave", "demandResponse"):
        return f"a running {hold.hold_type} event overrides the hold"
    if hold.kind != "temperature" or hold.heat_f is None or hold.cool_f is None:
        return "the running hold is not a temperature hold"
    if abs(hold.heat_f - heat) > 0.1 or abs(hold.cool_f - cool) > 0.1:
        return f"read-back {hold.heat_f}/{hold.cool_f}°F does not match requested {heat}/{cool}°F"
    if hold.end is None or hold.end > now + timedelta(hours=hours) + HOLD_END_SLACK:
        return "the running hold does not end within the requested hours"
    return None


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
        self._detail_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
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
        now = utcnow()
        for t in tstats:
            ident = str(t.get("identifier"))
            if all(k in t for k in ("events", "runtime", "settings", "program")):
                self._detail_cache[ident] = (now, t)
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

    async def _before(self, identifier: str) -> dict[str, Any]:
        cached = self._detail_cache.get(identifier)
        if cached and (utcnow() - cached[0]).total_seconds() <= DETAIL_CACHE_S:
            return cached[1]
        return await self._get_one(identifier, READBACK_INCLUDES)

    async def _post(self, identifier: str, payload: dict[str, Any]) -> None:
        body = {"selection": {"selectionType": "thermostats", "selectionMatch": identifier}, **payload}
        self._detail_cache.pop(identifier, None)
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
        from climate.control.guardrails import round_setpoint

        request: dict[str, object] = {"unit_key": req.unit_key, "heat_f": req.heat_f, "cool_f": req.cool_f,
                                      "hours": req.hours, "reason": req.reason}
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
            t_before = await self._before(ident)
            before = _write_state(t_before, ours)
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
            readback: dict[str, object] = {}
            why: str | None = "no read-back"
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(self._readback_delay_s)
                t_after = await self._get_one(ident, READBACK_INCLUDES)
                hold = _hold_of(t_after, None)
                readback = _write_state(t_after, None)
                why = _check_hold(hold, heat, cool, P.parse_utc(t_after.get("utcTime")) or utcnow(), hours)
                if why is None and hold is not None:
                    record = {"heat_f": heat, "cool_f": cool, "end": hold.end.isoformat() if hold.end else None,
                              "hours": hours, "written_at": sent_at.isoformat()}
                    await _db(_record_hold, req.unit_key, record)
                    readback = _write_state(t_after, record)  # now recognized as ours (set_by_us)
                    break
            if why is not None and post_error:
                why = f"{post_error}; {why}"
            return WriteResult(ok=why is None, channel="ecobee", before=before, request=request,
                               readback=readback, error=why)
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    async def resume_program(self, unit_key: str, reason: str) -> WriteResult:
        """resumeProgram (resumeAll=false) on a running HOLD only, then read back. A running
        vacation / demand-response event is never cancelled from here."""
        request: dict[str, object] = {"unit_key": unit_key, "reason": reason}
        before: dict[str, object] = {}
        try:
            inv = await _db(_load_inventory)
            ident = inv.ident_by_unit.get(unit_key)
            if not ident:
                return WriteResult(ok=False, channel="ecobee", request=request,
                                   error=f"unit {unit_key!r} is not mapped to an ecobee thermostat")
            ours = inv.our_holds.get(unit_key)
            t_before = await self._get_one(ident, READBACK_INCLUDES)
            before = _write_state(t_before, ours)
            current = _hold_of(t_before, ours)
            if current is None:
                await _db(_record_hold, unit_key, None)
                return WriteResult(ok=True, channel="ecobee", before=before, request={**request, "noop": True},
                                   readback=before)
            if current.hold_type in ("vacation", "demandResponse", "quickSave", "autoAway", "autoHome"):
                return WriteResult(ok=False, channel="ecobee", before=before, request=request,
                                   error=f"a running {current.hold_type} event is in effect; not resuming")
            function = {"type": "resumeProgram", "params": {"resumeAll": False}}
            request.update(identifier=ident, function=function)
            await self._post(ident, {"functions": [function]})
            t_after = await self._get_one(ident, READBACK_INCLUDES)
            readback = _write_state(t_after, ours)
            after = _hold_of(t_after, ours)
            if after is None or after.hold_type not in ("holdHours", "nextTransition", "indefinite", "dateTime"):
                await _db(_record_hold, unit_key, None)
                return WriteResult(ok=True, channel="ecobee", before=before, request=request, readback=readback)
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, readback=readback,
                               error="a hold is still running after resumeProgram")
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return WriteResult(ok=False, channel="ecobee", before=before, request=request, error=str(exc))

    async def ensure_settings(self) -> list[WriteResult]:
        """autoAway=false and followMeComfort=false on every unit (verified daily)."""
        wanted = {"autoAway": False, "followMeComfort": False}
        inv = await _db(_load_inventory)
        if not inv.ident_by_unit:
            return []
        results: dict[str, WriteResult] = {}
        try:
            tstats = {str(t.get("identifier")): t
                      for t in await self._get_thermostats(sorted(inv.unit_by_ident), {"includeSettings": True})}
        except (EcobeeApiError, EcobeeAuthError) as exc:
            return [WriteResult(ok=False, channel="ecobee", request={"unit_key": u, "identifier": i, "settings": wanted},
                                error=str(exc)) for u, i in inv.ident_by_unit.items()]
        to_verify: list[str] = []
        for unit, ident in inv.ident_by_unit.items():
            request: dict[str, object] = {"unit_key": unit, "identifier": ident, "settings": wanted}
            t = tstats.get(ident)
            if t is None:
                results[unit] = WriteResult(ok=False, channel="ecobee", request=request,
                                            error="thermostat not returned by ecobee")
                continue
            current = {k: (t.get("settings") or {}).get(k) for k in wanted}
            if all(P.parse_bool(v) is False for v in current.values()):
                results[unit] = WriteResult(ok=True, channel="ecobee", before=current,
                                            request={**request, "noop": True}, readback=current)
                continue
            try:
                await self._post(ident, {"thermostat": {"settings": wanted}})
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
                    results[inv.unit_by_ident[ident]].error = f"read-back failed: {exc}"
            for ident, t in after.items():
                unit = inv.unit_by_ident.get(ident)
                if unit is None or unit not in results:
                    continue
                readback = {k: (t.get("settings") or {}).get(k) for k in wanted}
                ok = all(P.parse_bool(v) is False for v in readback.values())
                res = results[unit]
                res.readback = readback
                res.ok = ok
                res.error = None if ok else "settings did not read back as autoAway=false, followMeComfort=false"
        return [results[u] for u in UNIT_KEYS if u in results]

    async def update_sensor_sets(self, unit_key: str, sets: dict[str, list[str]], reason: str) -> WriteResult:
        """Read-modify-write the program's comfort-setting sensor participation (fresh GET +
        revision check). Keep Home's set current: temperature holds use Home's sensors; that
        is the caller's job (only the climates in ``sets`` are written)."""
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
            rev0 = str(t.get("thermostatRev") or "")
            program = copy.deepcopy(t.get("program") or {})
            remote = t.get("remoteSensors") or []
            sp = P.parse_remote_sensors(unit_key, remote, inv.sensors, utcnow())
            names = {str(r.get("id")): str(r.get("name") or "") for r in remote}
            key_to_id = {k: i for i, k in sp.id_to_key.items()}
            before = {"revision": rev0, "sets": P.parse_sensor_sets(program, sp.id_to_key)}
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

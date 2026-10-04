"""Setup / onboarding (owner only): data source, location, ecobee account sign-in with MFA,
thermostat -> unit and sensor mapping, and the HomeKit pairing handshake.

HomeKit pairing is DB-mediated: the web UI moves ``homekit_devices.pairing_state``
(requested -> [service] awaiting_code -> code_submitted -> [service] paired) and the
host-networked homekit service does the HAP work. Responses never carry secrets: no ecobee
tokens, no pairing keys, no pairing codes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate import events
from climate.api.auth import OwnerDep, Role
from climate.api.routers.control import location_violations, require_unit, unprocessable
from climate.api.routers.status import HOMEKIT_ONLINE_S, has_secret, heartbeat_fresh
from climate.api.schemas import (
    DeviceBody,
    EcobeeLoginBody,
    EcobeeLoginResult,
    EcobeeMapBody,
    EcobeeMfaBody,
    EcobeeSetup,
    EcobeeThermostatOut,
    HomekitCodeBody,
    HomekitDeviceOut,
    HomekitPairBody,
    HomekitSetup,
    RoomOut,
    SensorMapBody,
    SensorOut,
    SetupState,
    SourceBody,
    UnitOut,
)
from climate.config import get_settings
from climate.sources import ecobee as ecobee_src
from climate.store import secrets
from climate.store.app_settings import LocationSettings, SourceSettings, get_raw, get_setting, put_setting
from climate.store.db import get_session, session_scope
from climate.store.orm import EcobeeThermostat, HomekitDevice, Room, Sensor, Unit
from climate.timeutil import utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["setup"])
T = TypeVar("T")
REFRESH_TOKEN_KEY = "ecobee_refresh_token"


# ---------------------------------------------------------------------------------------
# SetupState
# ---------------------------------------------------------------------------------------


def secrets_ok(session: Session) -> bool:
    """CLIMATE_SECRET_KEY is a usable Fernet key and still opens every stored secret."""
    key = get_settings().secret_key
    if not key:
        return False
    try:
        f = Fernet(key.encode())
        if f.decrypt(f.encrypt(b"probe")) != b"probe":
            return False
    except (ValueError, TypeError, InvalidToken):
        return False
    try:
        for k in secrets.list_secret_keys(session):
            secrets.get_secret(session, k)
    except secrets.SecretsUnavailable:
        return False
    return True


def _mfa_pending() -> tuple[bool, str | None]:
    try:
        pending, kind = ecobee_src.mfa_pending()
        return bool(pending), kind
    except Exception:  # noqa: BLE001 - an unavailable adapter means no challenge is pending
        log.warning("ecobee mfa_pending() failed", exc_info=True)
        return False, None


def _ecobee_setup(session: Session) -> EcobeeSetup:
    pending, mfa_type = _mfa_pending()
    status_raw = get_raw(session, "ecobee_status")
    last_error = None
    if isinstance(status_raw, dict):
        err = status_raw.get("last_error") or status_raw.get("error")
        last_error = str(err) if err else None
    thermostats = [
        EcobeeThermostatOut(
            identifier=t.identifier,
            name=t.name,
            model_number=t.model_number,
            unit_key=t.unit_key,
            sensors=[s for s in (t.sensors or []) if isinstance(s, dict)],
            last_seen_at=t.last_seen_at,
        )
        for t in session.execute(select(EcobeeThermostat).order_by(EcobeeThermostat.name)).scalars()
    ]
    return EcobeeSetup(
        signed_in=has_secret(session, REFRESH_TOKEN_KEY),
        mfa_pending=pending,
        mfa_type=mfa_type if pending else None,
        last_error=last_error,
        web_client_id=get_settings().ecobee_web_client_id,
        thermostats=thermostats,
    )


def device_out(d: HomekitDevice) -> HomekitDeviceOut:
    # pairing_code is deliberately absent: it is a transient secret for the homekit service.
    return HomekitDeviceOut(
        device_id=d.device_id,
        name=d.name,
        model=d.model,
        address=d.address,
        unpaired=bool((d.status_flags or 0) & 0x01),
        alias=d.alias,
        unit_key=d.unit_key,
        pairing_state=d.pairing_state,
        pairing_error=d.pairing_error,
        online=d.online,
        last_seen_at=d.last_seen_at,
        accessories=[a for a in d.accessories if isinstance(a, dict)] if isinstance(d.accessories, list) else None,
    )


def setup_state(session: Session, now: datetime | None = None) -> SetupState:
    now = now or utcnow()
    source = get_setting(session, "source", SourceSettings)
    online, _ = heartbeat_fresh(session, "homekit", now, HOMEKIT_ONLINE_S)
    devices = session.execute(select(HomekitDevice).order_by(HomekitDevice.name, HomekitDevice.device_id)).scalars()
    return SetupState(
        source=source,
        location=get_setting(session, "location", LocationSettings),
        ecobee=_ecobee_setup(session),
        homekit=HomekitSetup(enabled=source.homekit_enabled, service_online=online, devices=[device_out(d) for d in devices]),
        units=[
            UnitOut(key=u.key, name=u.name, thermostat_room_key=u.thermostat_room_key, thermostat_model=u.thermostat_model,
                    ecobee_identifier=u.ecobee_identifier, homekit_device_id=u.homekit_device_id, equipment=u.equipment or {})
            for u in session.execute(select(Unit).order_by(Unit.sort)).scalars()
        ],
        rooms=[
            RoomOut(key=r.key, name=r.name, unit_key=r.unit_key, floor=r.floor, has_sensor=r.has_sensor,
                    is_sleep_room=r.is_sleep_room, has_comfort_target=r.has_comfort_target, notes=r.notes)
            for r in session.execute(select(Room).order_by(Room.sort)).scalars()
        ],
        sensors=[
            SensorOut(key=s.key, room_key=s.room_key, unit_key=s.unit_key, kind=s.kind, name=s.name,
                      has_humidity=s.has_humidity, has_occupancy=s.has_occupancy,
                      ecobee_sensor_id=s.ecobee_sensor_id, homekit_aid=s.homekit_aid)
            for s in session.execute(select(Sensor).order_by(Sensor.sort)).scalars()
        ],
        secrets_ok=secrets_ok(session),
    )


def _commit_state(session: Session, event: str | None = "status") -> SetupState:
    session.flush()
    if event:
        events.publish(session, event)
    out = setup_state(session)
    session.commit()
    return out


def _in_new_session(fn: Callable[[Session], T]) -> T:
    """For the async ecobee routes: run DB work in a worker thread with its own session."""
    with session_scope() as s:
        return fn(s)


# ---------------------------------------------------------------------------------------
# source / location
# ---------------------------------------------------------------------------------------


@router.get("/setup", response_model=SetupState)
def get_setup(_: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    return setup_state(session)


@router.post("/setup/source", response_model=SetupState)
def set_source(body: SourceBody, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    if body.kind == "ecobee" and not has_secret(session, REFRESH_TOKEN_KEY):
        raise HTTPException(http.HTTP_409_CONFLICT, "Sign in to ecobee before switching the source to ecobee.")
    current = get_setting(session, "source", SourceSettings)
    current.kind = body.kind
    current.homekit_enabled = body.homekit_enabled
    put_setting(session, "source", current, updated_by="owner")
    return _commit_state(session)


@router.put("/setup/location", response_model=SetupState)
def set_location(body: LocationSettings, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    violations = location_violations(body, prefix="")
    if violations:
        raise unprocessable(violations)
    put_setting(session, "location", body, updated_by="owner")
    return _commit_state(session)


# ---------------------------------------------------------------------------------------
# ecobee account
# ---------------------------------------------------------------------------------------


def _login_result(raw: object) -> EcobeeLoginResult:
    try:
        return EcobeeLoginResult.model_validate(raw)
    except ValidationError:
        log.error("ecobee sign-in returned an unexpected result shape")
        return EcobeeLoginResult(status="error", error="ecobee sign-in returned an unexpected result.")


def _publish_status(session: Session) -> None:
    events.publish(session, "status")


@router.post("/setup/ecobee/login", response_model=EcobeeLoginResult)
async def ecobee_login(body: EcobeeLoginBody, _: Role = OwnerDep) -> EcobeeLoginResult:
    try:
        raw = await ecobee_src.start_login(body.email.strip(), body.password)
    except ecobee_src.EcobeeAuthError as exc:
        return EcobeeLoginResult(status="error", error=str(exc) or "ecobee sign-in failed.")
    except Exception as exc:  # noqa: BLE001 - report, never echo credentials
        log.error("ecobee sign-in failed unexpectedly (%s)", type(exc).__name__)
        return EcobeeLoginResult(status="error", error="ecobee sign-in failed unexpectedly; see the server log.")
    result = _login_result(raw)
    if result.status == "signed_in":
        await asyncio.to_thread(_in_new_session, _publish_status)
    return result


@router.post("/setup/ecobee/mfa", response_model=EcobeeLoginResult)
async def ecobee_mfa(body: EcobeeMfaBody, _: Role = OwnerDep) -> EcobeeLoginResult:
    pending, _kind = _mfa_pending()
    if not pending:
        raise HTTPException(http.HTTP_409_CONFLICT, "No ecobee sign-in is waiting for a code; start the sign-in again.")
    try:
        raw = await ecobee_src.submit_mfa(body.code)
    except ecobee_src.EcobeeAuthError as exc:
        return EcobeeLoginResult(status="error", error=str(exc) or "The code was not accepted.")
    except Exception as exc:  # noqa: BLE001
        log.error("ecobee MFA failed unexpectedly (%s)", type(exc).__name__)
        return EcobeeLoginResult(status="error", error="ecobee sign-in failed unexpectedly; see the server log.")
    result = _login_result(raw)
    if result.status == "signed_in":
        await asyncio.to_thread(_in_new_session, _publish_status)
    return result


@router.post("/setup/ecobee/signout", response_model=SetupState)
async def ecobee_signout(_: Role = OwnerDep) -> SetupState:
    try:
        await ecobee_src.sign_out()
    except Exception as exc:  # noqa: BLE001 - forgetting the token is what matters
        log.error("ecobee sign_out failed (%s); deleting the stored token directly", type(exc).__name__)
        await asyncio.to_thread(_in_new_session, lambda s: secrets.delete_secret(s, REFRESH_TOKEN_KEY))
    return await asyncio.to_thread(_in_new_session, _commit_state)


@router.post("/setup/ecobee/map", response_model=SetupState)
def ecobee_map(body: EcobeeMapBody, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    tstat = session.get(EcobeeThermostat, body.identifier)
    if tstat is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown ecobee thermostat {body.identifier!r}.")
    if body.unit_key is None:
        for u in session.execute(select(Unit).where(Unit.ecobee_identifier == tstat.identifier)).scalars():
            u.ecobee_identifier = None
        tstat.unit_key = None
        return _commit_state(session)
    unit = require_unit(session, body.unit_key)
    if unit.ecobee_identifier not in (None, tstat.identifier):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            f"{unit.name} is already mapped to thermostat {unit.ecobee_identifier}; unmap that one first.",
        )
    # A thermostat maps to one unit: release its previous unit (and any stale row pointing here).
    for u in session.execute(
        select(Unit).where(Unit.ecobee_identifier == tstat.identifier, Unit.key != unit.key)
    ).scalars():
        u.ecobee_identifier = None
    for t in session.execute(
        select(EcobeeThermostat).where(EcobeeThermostat.unit_key == unit.key, EcobeeThermostat.identifier != tstat.identifier)
    ).scalars():
        t.unit_key = None
    session.flush()
    tstat.unit_key = unit.key
    unit.ecobee_identifier = tstat.identifier
    if tstat.model_number:
        unit.thermostat_model = tstat.model_number
    return _commit_state(session)


@router.post("/setup/sensors/map", response_model=SetupState)
def sensors_map(body: SensorMapBody, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    """Fields left out are unchanged; an explicit null unmaps."""
    sensor = session.get(Sensor, body.sensor_key)
    if sensor is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown sensor {body.sensor_key!r}.")
    fields = body.model_fields_set - {"sensor_key"}
    if not fields:
        raise unprocessable([("", "Give ecobee_sensor_id and/or homekit_aid (null unmaps).")])
    if "ecobee_sensor_id" in fields:
        new_id = (body.ecobee_sensor_id or "").strip() or None
        if new_id is not None:
            other = session.execute(
                select(Sensor.key).where(
                    Sensor.unit_key == sensor.unit_key, Sensor.ecobee_sensor_id == new_id, Sensor.key != sensor.key
                )
            ).scalar_one_or_none()
            if other:
                raise HTTPException(http.HTTP_409_CONFLICT, f"ecobee sensor {new_id} is already mapped to {other}.")
        sensor.ecobee_sensor_id = new_id
    if "homekit_aid" in fields:
        if body.homekit_aid is not None:
            other = session.execute(
                select(Sensor.key).where(
                    Sensor.unit_key == sensor.unit_key, Sensor.homekit_aid == body.homekit_aid, Sensor.key != sensor.key
                )
            ).scalar_one_or_none()
            if other:
                raise HTTPException(http.HTTP_409_CONFLICT, f"HomeKit accessory {body.homekit_aid} is already mapped to {other}.")
        sensor.homekit_aid = body.homekit_aid
    return _commit_state(session)


# ---------------------------------------------------------------------------------------
# HomeKit pairing handshake
# ---------------------------------------------------------------------------------------


def _require_device(session: Session, device_id: str) -> HomekitDevice:
    dev = session.get(HomekitDevice, device_id.strip().lower())
    if dev is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown HomeKit device {device_id!r}.")
    return dev


@router.post("/setup/homekit/pair", response_model=SetupState)
def homekit_pair(body: HomekitPairBody, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    dev = _require_device(session, body.device_id)
    require_unit(session, body.unit_key)
    if dev.pairing_state not in ("none", "failed"):
        raise HTTPException(http.HTTP_409_CONFLICT, f"{dev.name} is already {dev.pairing_state.replace('_', ' ')}.")
    if not dev.online:
        raise HTTPException(http.HTTP_409_CONFLICT, f"{dev.name} is not on the network right now.")
    if not (dev.status_flags or 0) & 0x01:
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            f"{dev.name} is paired with another controller (Apple Home?). Remove it there first, then try again.",
        )
    alias_owner = session.execute(
        select(HomekitDevice.device_id).where(HomekitDevice.alias == body.alias, HomekitDevice.device_id != dev.device_id)
    ).scalar_one_or_none()
    if alias_owner:
        raise HTTPException(http.HTTP_409_CONFLICT, f"The alias {body.alias!r} is used by {alias_owner}.")
    unit_owner = session.execute(
        select(HomekitDevice.device_id).where(
            HomekitDevice.unit_key == body.unit_key,
            HomekitDevice.device_id != dev.device_id,
            HomekitDevice.pairing_state.not_in(("none", "failed")),
        )
    ).scalar_one_or_none()
    if unit_owner:
        raise HTTPException(http.HTTP_409_CONFLICT, f"Unit {body.unit_key} already has a HomeKit device ({unit_owner}).")
    dev.alias = body.alias
    dev.unit_key = body.unit_key
    dev.pairing_state = "requested"
    dev.pairing_code = None
    dev.pairing_error = None
    dev.updated_at = utcnow()
    return _commit_state(session, "homekit")


@router.post("/setup/homekit/code", response_model=SetupState)
def homekit_code(body: HomekitCodeBody, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    dev = _require_device(session, body.device_id)
    if dev.pairing_state != "awaiting_code":
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            f"{dev.name} is not waiting for a code (state: {dev.pairing_state.replace('_', ' ')}).",
        )
    dev.pairing_code = body.code
    dev.pairing_state = "code_submitted"
    dev.pairing_error = None
    dev.updated_at = utcnow()
    return _commit_state(session, "homekit")


@router.post("/setup/homekit/unpair", response_model=SetupState)
def homekit_unpair(body: DeviceBody, _: Role = OwnerDep, session: Session = Depends(get_session)) -> SetupState:
    dev = _require_device(session, body.device_id)
    if dev.pairing_state in ("none", "unpair_requested"):
        raise HTTPException(http.HTTP_409_CONFLICT, f"{dev.name} is not paired.")
    dev.pairing_state = "unpair_requested"
    dev.pairing_code = None
    dev.updated_at = utcnow()
    return _commit_state(session, "homekit")

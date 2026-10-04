"""Entry point of the host-networked HomeKit service: ``python -m climate.collector.homekit_service``.

Loops: mDNS discovery -> homekit_devices; the pairing handshake driven by the web UI
(pairing_state requested -> start pair-setup -> awaiting_code; code_submitted -> finish ->
save pairing encrypted -> paired; unpair_requested -> remove pairing); for each pairing:
populate accessories, map aids to sensors (by name), subscribe 'ev' characteristics, poll
every 60 s in batches of <= 49, push readings via collector.ingest.ingest_live_readings;
while a unit's live_units snapshot is more than 10 minutes old (ecobee cloud down) write a
HomeKit-derived snapshot there instead (never over a newer one) and, while the cloud circuit
is open, log a change someone made at the thermostat that HomeKit can see (a person's hold:
the controller stands aside); execute queued control_actions with channel 'homekit' (fail
'sent' rows a crashed run left behind, and controller rows queued before such a change);
heartbeat 'homekit' every loop.

Runs only while ``SourceSettings.homekit_enabled`` (otherwise it re-checks every 60 s). One
device's failure never stops the loop. Pairing keys live only in ``secrets``
('homekit_pairing:<alias>', Fernet) and in memory; they are never logged or written to a file.
Database work is sync SQLAlchemy run in a worker thread; HomeKit callbacks only queue work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate import events, notify
from climate.collector import ingest
from climate.sources.base import KNOWN_HOLD_TYPES, PROTECTED_EVENTS, SensorReading, UnitSnapshot, WriteResult
from climate.sources.homekit import (
    BAD_CODE_FORMAT_MSG,
    CLIMATE_CODES,
    CODE_RE,
    NO_PENDING_MSG,
    REPAIR_MSG,
    THERMOSTAT_AID,
    DiscoveredDevice,
    HandChange,
    HomekitBridge,
    HomekitError,
    KnownHolds,
    ReadResult,
    SensorRef,
    build_aid_map,
    comfort_targets_from_values,
    detect_hand_change,
    hold_window,
    readings_from_values,
    snapshot_from_values,
)
from climate.state import MANUAL_KIND
from climate.store import secrets
from climate.store.app_settings import (
    ControlSettings,
    HardLimits,
    LocationSettings,
    SourceSettings,
    beat,
    get_setting,
)
from climate.store.db import session_scope
from climate.store.orm import ControlAction, HomekitDevice, LiveUnit, Room, Sensor, Unit
from climate.timeutil import utcnow
from climate.utility.events import load_active

log = logging.getLogger("climate.homekit")

SECRET_PREFIX = "homekit_pairing:"
SERVICE = "homekit"
SENT_MAX_AGE = timedelta(minutes=15)  # a 'sent' row older than this was left by a crashed run
CLOUD_STALE_AFTER = timedelta(minutes=10)  # live snapshot older than this: HomeKit writes its own
# Queued kinds (control_actions.request.kind) that resume the schedule; 'resume_program' is
# the controller's name for it (older rows), 'clear_hold' the documented one.
CLEAR_HOLD_KINDS = ("clear_hold", "resume_program")
# A person's change seen over HomeKit is logged as a control_actions row by this actor, with
# request.kind MANUAL_KIND (state.py turns it into the unit's person hold) and this source.
HAND_ACTOR = "homekit_service"
HAND_SOURCE = "homekit"
HAND_HOLD_TYPE = "homekit_manual"
NOT_SENT_MSG = "Not sent: someone changed the thermostat by hand"
HOLD_ACTIONS = ("set_hold", "resume_program")  # thermostat writes (as in climate.state)
LANDED = ("verified", "sent")  # a write that reached (or is reaching) the thermostat
# Our HomeKit hold counts as running until this long before its end (the thermostat's clock
# may switch back to the schedule a little early).
OUR_END_SLACK = timedelta(minutes=5)
KEYS_MISSING_MSG = (
    "The saved pairing keys are missing from the database. Choose Disconnect from HomeKit on the thermostat, "
    "then pair again."
)
KEYS_UNREADABLE_MSG = (
    "The saved pairing keys cannot be decrypted (CLIMATE_SECRET_KEY changed or missing). Restore the key, or "
    "unpair and pair again."
)

SessionFactory = Callable[[], AbstractContextManager[Session]]
T = TypeVar("T")


def secret_key(alias: str) -> str:
    return f"{SECRET_PREFIX}{alias}"


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


# ---------------------------------------------------------------------------------------
# database operations (sync; each runs in one transaction). Shared with scripts/pair_ecobee.py
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceRow:
    device_id: str
    name: str
    alias: str | None
    unit_key: str | None
    pairing_state: str
    pairing_code: str | None
    pairing_error: str | None


def load_device_rows(session: Session) -> list[DeviceRow]:
    rows = session.execute(select(HomekitDevice).order_by(HomekitDevice.device_id)).scalars()
    return [
        DeviceRow(d.device_id, d.name, d.alias, d.unit_key, d.pairing_state, d.pairing_code, d.pairing_error)
        for d in rows
    ]


def set_pairing_state(
    session: Session,
    device_id: str,
    state: str,
    *,
    expect: list[str] | None = None,
    error: str | None = None,
    clear_code: bool = True,
) -> bool:
    """Move a device's handshake state (only from ``expect`` states, so a newer action from
    the web UI is never clobbered). Publishes 'homekit'. Returns True if a row changed."""
    values: dict[str, Any] = {"pairing_state": state, "pairing_error": error, "updated_at": func.now()}
    if clear_code:
        values["pairing_code"] = None
    stmt = (
        update(HomekitDevice)
        .where(HomekitDevice.device_id == device_id)
        .execution_options(synchronize_session=False)
    )
    if expect:
        stmt = stmt.where(HomekitDevice.pairing_state.in_(expect))
    changed = session.execute(stmt.values(**values)).rowcount > 0  # type: ignore[attr-defined]
    if changed:
        events.publish(session, "homekit")
    return changed


def set_pairing_error(session: Session, device_id: str, error: str | None) -> bool:
    row = session.get(HomekitDevice, device_id)
    if row is None or row.pairing_error == error:
        return False
    row.pairing_error = error
    row.updated_at = utcnow()
    events.publish(session, "homekit")
    return True


def upsert_discovered(
    session: Session, devices: list[DiscoveredDevice], loaded_device_ids: set[str], now: datetime
) -> bool:
    """Upsert what mDNS shows. ``online`` of a loaded pairing is owned by its polls."""
    existing = {d.device_id: d for d in session.execute(select(HomekitDevice)).scalars()}
    changed = False
    seen: set[str] = set()
    for dev in devices:
        seen.add(dev.id)
        row = existing.get(dev.id)
        if row is None:
            session.add(
                HomekitDevice(
                    device_id=dev.id, name=dev.name, model=dev.model, category=dev.category, address=dev.address,
                    port=dev.port, status_flags=dev.status_flags, config_num=dev.config_num, online=True,
                    last_seen_at=now, pairing_state="none", updated_at=now,
                )
            )
            changed = True
            continue
        dirty = False
        for attr, value in (
            ("name", dev.name), ("model", dev.model), ("category", dev.category), ("address", dev.address),
            ("port", dev.port), ("status_flags", dev.status_flags), ("config_num", dev.config_num),
        ):
            if getattr(row, attr) != value:
                setattr(row, attr, value)
                dirty = True
        if dev.id not in loaded_device_ids and not row.online:
            row.online = True
            dirty = True
        row.last_seen_at = now
        if dirty:
            row.updated_at = now
            changed = True
    for device_id, row in existing.items():
        if device_id not in seen and device_id not in loaded_device_ids and row.online:
            row.online = False
            row.updated_at = now
            changed = True
    if changed:
        session.flush()
        events.publish(session, "homekit")
    return changed


def save_pairing(
    session: Session,
    device_id: str,
    alias: str,
    unit_key: str,
    pairing_data: dict[str, Any],
    device: DiscoveredDevice | None,
    now: datetime,
) -> None:
    """Persist a NEW pairing in one transaction: the encrypted keys first, then the device row
    ('paired', code cleared) and ``units.homekit_device_id``. Raises on any problem, which
    rolls everything back (the caller then removes the pairing from the thermostat)."""
    if session.get(Unit, unit_key) is None:
        raise ValueError(f"unknown unit {unit_key!r}")
    secrets.put_secret_json(session, secret_key(alias), pairing_data)
    row = session.get(HomekitDevice, device_id)
    if row is None:
        row = HomekitDevice(device_id=device_id, name=device.name if device else alias, pairing_state="none",
                            online=True)
        session.add(row)
    if device is not None:
        row.name, row.model, row.category = device.name, device.model, device.category
        row.address, row.port = device.address, device.port
        row.status_flags, row.config_num = device.status_flags, device.config_num
        row.online, row.last_seen_at = True, now
    row.alias = alias
    row.unit_key = unit_key
    row.pairing_state = "paired"
    row.pairing_code = None
    row.pairing_error = None
    row.updated_at = now
    session.flush()
    session.execute(
        update(Unit).where(Unit.homekit_device_id == device_id, Unit.key != unit_key).values(homekit_device_id=None)
    )
    session.execute(update(Unit).where(Unit.key == unit_key).values(homekit_device_id=device_id))
    events.publish(session, "homekit")


def forget_pairing(session: Session, device_id: str, alias: str | None, note: str | None = None) -> None:
    """After an unpair: delete the keys, unlink the unit, reset the device row to 'none'."""
    if alias:
        secrets.delete_secret(session, secret_key(alias))
    session.execute(update(Unit).where(Unit.homekit_device_id == device_id).values(homekit_device_id=None))
    session.execute(
        update(HomekitDevice)
        .where(HomekitDevice.device_id == device_id)
        .values(pairing_state="none", pairing_code=None, pairing_error=note, alias=None, unit_key=None,
                accessories=None, updated_at=func.now())
    )
    events.publish(session, "homekit")


def get_pairing_secret(session: Session, alias: str) -> dict[str, Any] | None:
    data = secrets.get_secret_json(session, secret_key(alias))
    return data if isinstance(data, dict) else None


def store_inventory(
    session: Session, device_id: str, unit_key: str | None, inventory: list[dict[str, Any]]
) -> dict[int, str]:
    """Save the accessory inventory and map aids to this unit's sensors. Sets
    ``sensors.homekit_aid`` for name matches when it is empty or points at an aid this
    pairing no longer has (owner mappings that still exist win). Returns {aid: sensor_key}."""
    row = session.get(HomekitDevice, device_id)
    if row is not None:
        row.accessories = inventory
        row.updated_at = utcnow()
    aid_map: dict[int, str] = {}
    if unit_key:
        sensors = list(
            session.execute(select(Sensor).where(Sensor.unit_key == unit_key, Sensor.is_active.is_(True))).scalars()
        )
        rooms = {r.key: r.name for r in session.execute(select(Room)).scalars()}
        refs = [SensorRef(s.key, s.name, s.kind, rooms.get(s.room_key, s.name)) for s in sensors]
        existing = {s.key: s.homekit_aid for s in sensors if s.homekit_aid is not None}
        aid_map = build_aid_map(inventory, unit_key, existing, refs)
        aid_by_key = {key: aid for aid, key in aid_map.items()}
        for s in sensors:
            aid = aid_by_key.get(s.key)
            if aid is not None and s.homekit_aid != aid:
                s.homekit_aid = aid
    session.flush()
    events.publish(session, "homekit")
    return aid_map


def _ingest(session: Session, readings: list[SensorReading]) -> None:
    """ingest_live_readings inside a savepoint: a failure there never loses the device update."""
    if not readings:
        return
    try:
        with session.begin_nested():
            ingest.ingest_live_readings(session, readings, source="homekit")
    except Exception as exc:  # noqa: BLE001
        log.warning("homekit: ingest of %d readings failed: %s", len(readings), _short(exc))


def ingest_readings(session: Session, readings: list[SensorReading]) -> None:
    _ingest(session, readings)


def record_poll(
    session: Session, device_id: str, res: ReadResult, readings: list[SensorReading], now: datetime
) -> bool:
    """Write a poll's readings and the device's online / re-pair state. True if the row changed."""
    if res.ok:
        _ingest(session, readings)
    row = session.get(HomekitDevice, device_id)
    if row is None:
        return False
    changed = False
    online = bool(res.available and not res.needs_repair)
    if row.online != online:
        row.online = online
        changed = True
    if res.ok:
        row.last_seen_at = now
    if res.needs_repair and row.pairing_error != REPAIR_MSG:
        row.pairing_error = REPAIR_MSG
        changed = True
    elif res.ok and row.pairing_error == REPAIR_MSG:
        row.pairing_error = None
        changed = True
    if changed:
        row.updated_at = now
        events.publish(session, "homekit")
    return changed


@dataclass(frozen=True)
class ActionJob:
    id: int
    unit_key: str
    action: str
    request: dict[str, Any]


def claim_queued_actions(session: Session, now: datetime, max_age: timedelta) -> list[ActionJob]:
    """Claim channel='homekit' rows in status 'queued' (-> 'sent'); expire stale ones, and
    fail any row not queued by the owner when someone changed the thermostat by hand after it
    was queued (``hand_change_after``): the controller decided it before it knew."""
    rows = (
        session.execute(
            select(ControlAction)
            .where(ControlAction.channel == "homekit", ControlAction.status == "queued")
            .order_by(ControlAction.ts, ControlAction.id)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    jobs: list[ActionJob] = []
    for r in rows:
        if r.ts is not None and now - r.ts > max_age:
            r.status = "failed"
            r.error = f"expired: not executed within {int(max_age.total_seconds() // 60)} minutes of being queued"
            r.completed_at = now
        elif r.actor != "owner" and (hand := hand_change_after(session, r)) is not None:
            r.status = "failed"
            r.error = (f"{NOT_SENT_MSG} after this was queued (seen over HomeKit, action {hand.id}); the "
                       "controller stands aside until the ecobee cloud is back.")
            r.completed_at = now
        else:
            r.status = "sent"
            jobs.append(ActionJob(r.id, r.unit_key, r.action, dict(r.request or {})))
        events.publish(session, "action", r.id)
    return jobs


def _latest_hand_change(unit_key: str) -> Select[tuple[ControlAction]]:
    """SELECT of this service's newest hand-change row for the unit."""
    return (
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.channel == "homekit",
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == MANUAL_KIND,
            ControlAction.request["source"].astext == HAND_SOURCE,
        )
        .order_by(ControlAction.id.desc())
        .limit(1)
    )


def _written_since(session: Session, unit_key: str, row_id: int) -> bool:
    """Did a hold write or resume reach the unit's thermostat after control_actions row ``row_id``?"""
    return session.execute(
        select(ControlAction.id).where(
            ControlAction.unit_key == unit_key, ControlAction.id > row_id, ControlAction.status.in_(LANDED),
            ControlAction.action.in_(HOLD_ACTIONS), ControlAction.channel != "none",
        ).limit(1)
    ).scalar_one_or_none() is not None


def hand_change_after(session: Session, queued: ControlAction) -> ControlAction | None:
    """The newest hand-change row for ``queued``'s unit logged after that row was queued (by
    id, or by time: a controller tick that started before the change was logged), while it
    is still the latest word on the unit (nothing was written to the thermostat since)."""
    after = ControlAction.id > queued.id
    if queued.ts is not None:
        after = or_(after, ControlAction.ts > queued.ts)
    hand = session.execute(_latest_hand_change(queued.unit_key).where(after)).scalar_one_or_none()
    if hand is None or _written_since(session, queued.unit_key, hand.id):
        return None
    return hand


def expire_stuck_actions(session: Session, now: datetime, max_age: timedelta = SENT_MAX_AGE) -> int:
    """Fail channel='homekit' rows still 'sent' more than ``max_age`` after they were queued.

    A row is 'sent' only while this service executes it, which takes seconds; an old one was
    claimed by a run that crashed or restarted mid-write. Left alone it would block the unit
    forever (the controller queues nothing new while a HomeKit row is queued or sent)."""
    rows = (
        session.execute(
            select(ControlAction)
            .where(ControlAction.channel == "homekit", ControlAction.status == "sent",
                   ControlAction.ts < now - max_age)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for r in rows:
        r.status = "failed"
        r.error = (
            f"expired: claimed by the HomeKit service but not finished within {int(max_age.total_seconds() // 60)} "
            "minutes (the service probably restarted mid-write); whether it reached the thermostat is unknown"
        )
        r.completed_at = now
        events.publish(session, "action", r.id)
    if rows:
        log.warning("homekit: %d stuck 'sent' action(s) marked failed", len(rows))
    return len(rows)


def _our_climate_hold(session: Session, unit_key: str, now: datetime) -> tuple[str, datetime] | None:
    """(climate, until) of our newest verified HomeKit climate hold, unless a later verified
    HomeKit resume cleared it or it has ended."""
    row = session.execute(
        select(ControlAction)
        .where(ControlAction.unit_key == unit_key, ControlAction.channel == "homekit",
               ControlAction.status == "verified", ControlAction.action.in_(("set_hold", "resume_program")))
        .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    req = (row.request or {}) if row is not None else {}
    if req.get("kind") != "climate_hold" or req.get("climate") not in CLIMATE_CODES:
        return None
    until = _parse_until(req.get("until"))
    return (str(req["climate"]), until) if until is not None and until > now else None


@dataclass(frozen=True)
class _SnapshotWrite:
    written: bool
    previous: UnitSnapshot | None = None  # the live snapshot this one replaced
    previous_source: str | None = None
    circuit_open: bool = False  # the ecobee cloud circuit breaker is open


def _write_snapshot(
    session: Session, unit_key: str, values: dict[int, dict[str, Any]], aid_map: dict[int, str], now: datetime,
    stale_after: timedelta,
) -> _SnapshotWrite:
    src = get_setting(session, "source", SourceSettings)
    if src.kind != "ecobee":
        return _SnapshotWrite(False)
    if session.get(Unit, unit_key) is None:
        return _SnapshotWrite(False)
    row = session.execute(select(LiveUnit).where(LiveUnit.unit_key == unit_key).with_for_update()).scalar_one_or_none()
    previous: UnitSnapshot | None = None
    if row is not None:
        if row.ts > now:
            return _SnapshotWrite(False)  # a newer snapshot is already there
        if row.source != "homekit" and now - row.ts <= stale_after:
            return _SnapshotWrite(False)  # the cloud is current
        try:
            previous = UnitSnapshot.model_validate(row.snapshot)
        except ValidationError:
            previous = None
    previous_source = row.source if row is not None else None
    tz = get_setting(session, "location", LocationSettings).tz
    snap = snapshot_from_values(unit_key, values, aid_map, now, tz, previous=previous,
                                our_hold=_our_climate_hold(session, unit_key, now))
    stmt = insert(LiveUnit).values(unit_key=unit_key, ts=now, source="homekit", revision=None,
                                   snapshot=snap.model_dump(mode="json"))
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[LiveUnit.unit_key],
        set_={"ts": ex.ts, "source": ex.source, "revision": ex.revision, "snapshot": ex.snapshot},
        where=(LiveUnit.ts <= ex.ts) & or_(LiveUnit.source == "homekit", LiveUnit.ts < now - stale_after),
    )
    written = session.execute(stmt.execution_options(preserve_rowcount=True)).rowcount > 0  # type: ignore[attr-defined]
    if written:
        events.publish(session, "status")
    circuit_open = src.cloud_circuit_open_until is not None and src.cloud_circuit_open_until > now
    return _SnapshotWrite(written, previous, previous_source, circuit_open)


def write_homekit_snapshot(
    session: Session,
    unit_key: str,
    values: dict[int, dict[str, Any]],
    aid_map: dict[int, str],
    now: datetime,
    stale_after: timedelta = CLOUD_STALE_AFTER,
) -> bool:
    """Write a HomeKit-derived snapshot into live_units when the unit's live snapshot is older
    than ``stale_after`` (the ecobee cloud is down) or is already HomeKit's. Only in ecobee
    source mode. Never overwrites a NEWER snapshot or a current cloud one: the row is locked and
    the upsert re-checks both in SQL. Returns True when written."""
    return _write_snapshot(session, unit_key, values, aid_map, now, stale_after).written


@dataclass(frozen=True)
class FallbackResult:
    written: bool  # a HomeKit snapshot is now the unit's live snapshot
    hand_change_id: int | None = None  # the control_actions row logging a person's change


def homekit_fallback(
    session: Session,
    unit_key: str,
    values: dict[int, dict[str, Any]],
    aid_map: dict[int, str],
    now: datetime,
    stale_after: timedelta = CLOUD_STALE_AFTER,
) -> FallbackResult:
    """``write_homekit_snapshot``, then, when that snapshot was written while the ecobee cloud
    circuit is open (HomeKit is the live data path), ``record_hand_change``. A failure in the
    detection never loses the snapshot."""
    res = _write_snapshot(session, unit_key, values, aid_map, now, stale_after)
    if not res.written or not res.circuit_open:
        return FallbackResult(res.written)
    try:
        with session.begin_nested():
            hand_id = record_hand_change(session, unit_key, values, now, previous=res.previous,
                                         continuing=res.previous_source == "homekit")
    except Exception as exc:  # noqa: BLE001 - the snapshot stays; the next poll looks again
        log.warning("homekit: hand-change check for unit %s failed: %s", unit_key, _short(exc))
        hand_id = None
    return FallbackResult(True, hand_id)


def _hold_hours(session: Session) -> float:
    try:
        return float(get_setting(session, "control", ControlSettings).hold_hours)
    except ValidationError:
        return float(ControlSettings().hold_hours)


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _our_recent_holds(
    session: Session, unit_key: str, now: datetime,
) -> tuple[str | None, frozenset[str], list[tuple[float | None, float | None]]]:
    """What our own writes may have put on the thermostat: (the climate of our verified
    HomeKit climate hold still running, climates of newer HomeKit holds that may have landed,
    setpoints of our temperature holds still running). Reads the unit's hold writes and
    resumes (any channel, any actor of ours) from the newest back to the newest verified one,
    which replaced everything older. A failed row counts as maybe-landed only when it was
    read back (the write went out); rows failed before sending never reached the thermostat."""
    # (readback is JSONB: a write that never went out stores JSON null, not SQL NULL)
    went_out = or_(ControlAction.status.in_(LANDED),
                   (ControlAction.status == "failed") & (func.jsonb_typeof(ControlAction.readback) == "object"))
    rows = session.execute(
        select(ControlAction)
        .where(ControlAction.unit_key == unit_key, ControlAction.action.in_(HOLD_ACTIONS),
               ControlAction.channel != "none", went_out)
        .order_by(ControlAction.id.desc())
        .limit(20)
    ).scalars().all()
    hours = _hold_hours(session)
    climate: str | None = None
    maybe: set[str] = set()
    temps: list[tuple[float | None, float | None]] = []
    for r in rows:
        req = r.request if isinstance(r.request, dict) else {}
        if r.action == "set_hold" and req.get("kind") == "climate_hold" and req.get("climate") in CLIMATE_CODES:
            until = _parse_until(req.get("until"))
            if until is not None and r.status == "verified" and until - OUR_END_SLACK > now:
                climate = str(req["climate"])
            elif until is not None and until > now and r.status != "verified":
                maybe.add(str(req["climate"]))
        elif r.action == "set_hold":
            heat, cool = req.get("heat_f"), req.get("cool_f")
            if _number(heat) and _number(cool):
                h = req.get("hours")
                end = _parse_until(req.get("until")) or (r.completed_at or r.ts) + timedelta(
                    hours=float(h) if _number(h) and float(h) > 0 else hours)
                if end > now:
                    temps.append((float(heat), float(cool)))
        if r.status == "verified":
            break
    return climate, frozenset(maybe), temps


def known_holds(session: Session, unit_key: str, previous: UnitSnapshot | None, now: datetime) -> KnownHolds:
    """What may be on the unit's thermostat that is not a new change by a person: our own
    holds (``_our_recent_holds``); an ecobee event in force (a utility event whose clock
    window covers now, or a vacation / utility / unrecognised event the last live snapshot
    still carries); a temperature hold the last live snapshot carries that came from the
    cloud (ours, or a person's the controller already logged). A temperature hold HomeKit
    itself reported (start unknown, not ours) is not known: it is what this check is for."""
    climate, maybe, temps = _our_recent_holds(session, unit_key, now)
    event = any(e.unit_key == unit_key and e.covers(now) for e in load_active(session, now))
    hold = previous.hold if previous is not None else None
    if hold is not None and (hold.end is None or hold.end > now):
        unknown_event = hold.hold_type is not None and hold.hold_type not in KNOWN_HOLD_TYPES
        if hold.hold_type in PROTECTED_EVENTS or unknown_event:
            event = True
        elif hold.kind == "temperature" and (
            previous is not None and (previous.source != "homekit" or hold.start is not None or hold.set_by_us)
        ):
            temps.append((hold.heat_f, hold.cool_f))
    return KnownHolds(climate=climate, maybe_climates=maybe, temps=tuple(temps), event=event)


def hand_change_reason(change: HandChange, unit_name: str) -> str:
    where = f"on the {unit_name.lower()} thermostat (seen over HomeKit while the ecobee cloud is down)"
    tail = "the controller stands aside until the cloud is back."
    if change.kind == "comfort" and change.climate_ref:
        return f"Someone picked {change.climate_ref.capitalize()} by hand {where}; {tail}"
    temps = ", ".join(f"{side} {v:g}°F" for side, v in (("heat", change.heat_f), ("cool", change.cool_f))
                      if v is not None)
    held = f"a temperature hold ({temps})" if temps else "a temperature hold"
    return f"Someone set {held} by hand {where}; {tail}"


def record_hand_change(
    session: Session,
    unit_key: str,
    values: dict[int, dict[str, Any]],
    now: datetime,
    *,
    previous: UnitSnapshot | None,
    continuing: bool,
) -> int | None:
    """Log a person's change HomeKit shows (``detect_hand_change``) as ONE control_actions row
    (actor 'homekit_service', status 'skipped', rule 'hold_off', request.kind MANUAL_KIND,
    source 'homekit', until None): ``climate.state`` makes it the unit's person hold, so the
    controller stands aside until the cloud is back and shows the real hold state. Not logged
    again while the same change persists: while HomeKit stays the live path (``continuing``:
    the snapshot it replaced was HomeKit's too) and nothing was written to the thermostat
    since the last such row, an identical change is that row's. Returns the new row's id."""
    change = detect_hand_change(values, known_holds(session, unit_key, previous, now))
    if change is None:
        return None
    last = session.execute(_latest_hand_change(unit_key)).scalar_one_or_none()
    if (continuing and last is not None and change.same_as(last.request)
            and not _written_since(session, unit_key, last.id)):
        return None  # the same change, still showing: already logged
    unit = session.get(Unit, unit_key)
    tstat = values.get(THERMOSTAT_AID, {})
    targets = {c: t for c, t in comfort_targets_from_values(tstat).items() if any(v is not None for v in t.values())}
    row = ControlAction(
        ts=now, unit_key=unit_key, actor=HAND_ACTOR, mode="act", channel="homekit", action="set_hold",
        status="skipped", rule="hold_off", reason=hand_change_reason(change, unit.name if unit else unit_key),
        before={"current_mode": change.current_mode, "heat_sp_f": change.heat_f, "cool_sp_f": change.cool_f,
                "comfort_targets": targets},
        request={"kind": MANUAL_KIND, "source": HAND_SOURCE, "climate_ref": change.climate_ref,
                 "heat_f": change.heat_f, "cool_f": change.cool_f, "hold_type": HAND_HOLD_TYPE,
                 "start": now.isoformat(), "until": None},
        completed_at=now,
    )
    session.add(row)
    session.flush()
    events.publish(session, "action", row.id)
    return row.id


def comfort_violation(climate: str, targets: dict[str, float | None], limits: HardLimits) -> str | None:
    """Why a climate hold of ``climate`` must not be written, judged on that comfort setting's
    own heat/cool targets (read from the thermostat) against the hard limits; None when fine."""
    heat, cool = targets.get("heat_f"), targets.get("cool_f")
    if heat is None or cool is None:
        return (f"could not read the {climate} comfort setting's heat/cool targets from the thermostat, so a "
                f"{climate} hold would hold unknown temperatures; not written")
    problems: list[str] = []
    if not limits.min_heat_f <= heat <= limits.max_heat_f:
        problems.append(f"heat {heat:g}°F is outside {limits.min_heat_f:g}-{limits.max_heat_f:g}°F")
    if not limits.min_cool_f <= cool <= limits.max_cool_f:
        problems.append(f"cool {cool:g}°F is outside {limits.min_cool_f:g}-{limits.max_cool_f:g}°F")
    if cool - heat < limits.min_deadband_f:
        problems.append(f"cool - heat = {cool - heat:g}°F is below the {limits.min_deadband_f:g}°F deadband")
    if not problems:
        return None
    return (f"the {climate} comfort setting holds {heat:g}/{cool:g}°F, outside the hard limits "
            f"({'; '.join(problems)}); not written")


def finish_action(session: Session, action_id: int, result: WriteResult, now: datetime) -> None:
    r = session.get(ControlAction, action_id)
    if r is None:
        return
    r.status = "verified" if result.ok else "failed"
    if result.before:
        # what the thermostat showed before the write (timestamp, current mode, the comfort
        # targets a climate hold holds) next to the queuer's own 'before' (which wins on a clash)
        r.before = {**dict(result.before), **(r.before or {})}
    r.readback = None if result.readback is None else dict(result.readback)
    r.readback_ok = None if result.readback is None else bool(result.ok)
    r.error = result.error
    r.completed_at = now
    events.publish(session, "action", action_id)


def _homekit_enabled(session: Session) -> bool:
    return get_setting(session, "source", SourceSettings).homekit_enabled


def _tz_and_limits(session: Session) -> tuple[str, HardLimits]:
    return get_setting(session, "location", LocationSettings).tz, get_setting(session, "control", ControlSettings).limits


def _unit_devices(session: Session) -> dict[str, str | None]:
    return {u.key: u.homekit_device_id for u in session.execute(select(Unit)).scalars()}


def _parse_until(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)  # 3.11+ accepts a trailing Z
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------------------


@dataclass
class _AliasState:
    alias: str
    device_id: str
    unit_key: str | None
    aid_map: dict[int, str] = field(default_factory=dict)
    ready: bool = False  # inventory stored, aids mapped, subscribed
    next_poll: datetime | None = None  # None = due now
    next_inventory: datetime | None = None
    repair_alerted: bool = False


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


class HomekitService:
    def __init__(
        self,
        bridge: HomekitBridge | None = None,
        *,
        session_factory: SessionFactory = session_scope,
        clock: Callable[[], datetime] = utcnow,
        tick_seconds: float = 2.0,
        poll_seconds: float = 60.0,
        discover_seconds: float = 60.0,
        heartbeat_seconds: float = 30.0,
        disabled_recheck_seconds: float = 60.0,
        code_timeout_seconds: float = 300.0,
        action_max_age: timedelta = timedelta(minutes=10),
        sent_max_age: timedelta = SENT_MAX_AGE,
        cloud_stale_after: timedelta = CLOUD_STALE_AFTER,
    ) -> None:
        self.bridge = bridge if bridge is not None else HomekitBridge()
        self.bridge.on_event = self._on_event
        self._session_factory = session_factory
        self.clock = clock
        self.tick_s = tick_seconds
        self.poll_s = poll_seconds
        self.discover_s = discover_seconds
        self.heartbeat_s = heartbeat_seconds
        self.disabled_recheck_s = disabled_recheck_seconds
        self.code_timeout_s = code_timeout_seconds
        self.action_max_age = action_max_age
        self.sent_max_age = sent_max_age
        self.cloud_stale_after = cloud_stale_after
        self._states: dict[str, _AliasState] = {}
        self._event_aids: dict[str, set[int]] = {}
        self._reconnected: set[str] = set()
        self._unit_device: dict[str, str | None] = {}
        self._next_discover: datetime | None = None
        self._next_beat: datetime | None = None
        self._errors: dict[str, str] = {}

    # --- plumbing ---------------------------------------------------------------------

    async def _db(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        def run() -> T:
            with self._session_factory() as session:
                return fn(session, *args, **kwargs)

        return await asyncio.to_thread(run)

    async def _guard(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await fn()
        except Exception as exc:
            self._errors[name] = _short(exc)
            log.warning("homekit %s step failed: %s", name, _short(exc))
            log.debug("homekit %s step traceback", name, exc_info=True)

    def _on_event(self, alias: str, aids: set[int]) -> None:
        """Bridge hook, called synchronously from aiohomekit's listener: only queue work."""
        if aids:
            self._event_aids.setdefault(alias, set()).update(aids)
        else:
            self._reconnected.add(alias)  # (re)connected: poll soon

    # --- main loop --------------------------------------------------------------------

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        log.info("homekit service starting")
        try:
            while not stop.is_set():
                try:
                    enabled = await self._db(_homekit_enabled)
                except Exception as exc:  # noqa: BLE001
                    log.warning("homekit: cannot read settings: %s", _short(exc))
                    await _sleep(stop, self.disabled_recheck_s)
                    continue
                if not enabled:
                    if self.bridge.started:
                        log.info("HomeKit is disabled in settings; stopping the controller")
                        await self._stop_bridge()
                    await self._expire_stuck()
                    await self._beat(True, enabled=False)
                    await _sleep(stop, self.disabled_recheck_s)
                    continue
                if not self.bridge.started:
                    try:
                        await self.bridge.start()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("homekit: controller failed to start: %s", _short(exc))
                        await self._beat(False, enabled=True, error=_short(exc))
                        await _sleep(stop, self.disabled_recheck_s)
                        continue
                await self.step()
                await _sleep(stop, self.tick_s)
        finally:
            await self._stop_bridge()
            log.info("homekit service stopped")

    async def _stop_bridge(self) -> None:
        with contextlib.suppress(Exception):
            await self.bridge.stop()
        self._states.clear()
        self._event_aids.clear()
        self._reconnected.clear()

    async def step(self) -> None:
        """One pass. Every part is guarded so one failure never stops the others."""
        now = self.clock()
        self._errors = {}
        await self._guard("pairing", self.process_pairing)
        if self._next_discover is None or now >= self._next_discover:
            self._next_discover = now + timedelta(seconds=self.discover_s)
            await self._guard("discovery", self.sync_discovery)
        await self._guard("reconcile", self.reconcile)
        await self._guard("events", self.drain_events)
        await self._guard("poll", self.poll_due)
        await self._guard("actions", self.execute_actions)
        if self._next_beat is None or now >= self._next_beat:
            self._next_beat = now + timedelta(seconds=self.heartbeat_s)
            await self._beat(not self._errors, enabled=True)

    async def _beat(self, ok: bool, **detail: Any) -> None:
        aliases = self.bridge.aliases() if self.bridge.started else []
        info: dict[str, Any] = {
            "paired": len(aliases),
            "online": sum(1 for a in aliases if self.bridge.is_available(a)),
            **detail,
        }
        if self._errors:
            info["errors"] = dict(self._errors)
        try:
            await self._db(beat, SERVICE, ok, **info)
        except Exception as exc:  # noqa: BLE001
            log.warning("homekit: heartbeat failed: %s", _short(exc))

    # --- discovery --------------------------------------------------------------------

    async def sync_discovery(self) -> None:
        devices = await self.bridge.discover()
        loaded = {self.bridge.device_id(a) for a in self.bridge.aliases()}
        await self._db(upsert_discovered, devices, loaded, self.clock())

    # --- pairing handshake ------------------------------------------------------------

    async def process_pairing(self) -> None:
        rows = await self._db(load_device_rows)
        in_handshake = {r.device_id for r in rows if r.pairing_state in ("requested", "awaiting_code", "code_submitted")}
        for device_id in self.bridge.pending_device_ids():
            if device_id not in in_handshake:
                await self.bridge.cancel_pairing(device_id)
        for r in rows:
            handler = {
                "requested": self._begin,
                "awaiting_code": self._check_awaiting,
                "code_submitted": self._finish,
                "unpair_requested": self._unpair,
            }.get(r.pairing_state)
            if handler is None:
                continue
            try:
                await handler(r)
            except Exception as exc:  # noqa: BLE001
                log.warning("homekit: handshake for %s (%s) failed: %s", r.device_id, r.pairing_state, _short(exc))
                fallback = "paired" if r.pairing_state == "unpair_requested" else "failed"
                await self._db(set_pairing_state, r.device_id, fallback, expect=[r.pairing_state],
                               error=f"Unexpected error ({_short(exc)}). Try again.")

    async def _begin(self, r: DeviceRow) -> None:
        if not r.alias or not r.unit_key:
            await self._db(set_pairing_state, r.device_id, "failed", expect=["requested"],
                           error="Choose a name (alias) and a unit before pairing.")
            return
        try:
            await self.bridge.begin_pairing(r.device_id, r.alias)
        except HomekitError as exc:
            await self._db(set_pairing_state, r.device_id, "failed", expect=["requested"], error=str(exc))
            return
        moved = await self._db(set_pairing_state, r.device_id, "awaiting_code", expect=["requested"])
        if not moved:
            await self.bridge.cancel_pairing(r.device_id)

    async def _check_awaiting(self, r: DeviceRow) -> None:
        age = self.bridge.pending_age(r.device_id)
        if age is None:
            await self._db(set_pairing_state, r.device_id, "failed", expect=["awaiting_code"], error=NO_PENDING_MSG)
        elif age > self.code_timeout_s:
            await self.bridge.cancel_pairing(r.device_id)
            minutes = max(1, round(self.code_timeout_s / 60))
            await self._db(set_pairing_state, r.device_id, "failed", expect=["awaiting_code"],
                           error=f"Timed out waiting for the code ({minutes} min). Start pairing again.")

    async def _finish(self, r: DeviceRow) -> None:
        code = (r.pairing_code or "").strip()
        if not self.bridge.has_pending(r.device_id):
            await self._db(set_pairing_state, r.device_id, "failed", expect=["code_submitted"], error=NO_PENDING_MSG)
            return
        if not CODE_RE.fullmatch(code):
            await self._db(set_pairing_state, r.device_id, "awaiting_code", expect=["code_submitted"],
                           error=BAD_CODE_FORMAT_MSG)
            return
        if not r.alias or not r.unit_key:
            await self.bridge.cancel_pairing(r.device_id)
            await self._db(set_pairing_state, r.device_id, "failed", expect=["code_submitted"],
                           error="Choose a name (alias) and a unit before pairing.")
            return
        alias, unit_key = r.alias, r.unit_key

        async def save(data: dict[str, Any]) -> None:
            await self._db(save_pairing, r.device_id, alias, unit_key, data, None, self.clock())

        try:
            await self.bridge.finish_pairing(r.device_id, code, save=save)
        except HomekitError as exc:
            await self._db(set_pairing_state, r.device_id, "failed", expect=["code_submitted"], error=str(exc))
            return
        log.info("homekit: %s paired as %r for unit %s", r.device_id, alias, unit_key)
        # reconcile() loads it from the database later in this same pass.

    async def _unpair(self, r: DeviceRow) -> None:
        alias = r.alias
        self._states.pop(alias or "", None)
        if alias and not self.bridge.is_loaded(alias):
            try:
                data = await self._db(get_pairing_secret, alias)
            except secrets.SecretsUnavailable:
                await self._db(forget_pairing, r.device_id, alias,
                               "Removed here, but the saved keys could not be decrypted, so the thermostat may still "
                               "consider itself paired: choose Disconnect from HomeKit on it before pairing again.")
                return
            if data is not None:
                await self.bridge.load(alias, data)
        if alias and self.bridge.is_loaded(alias):
            try:
                await self.bridge.remove(alias)
            except HomekitError as exc:
                await self._db(set_pairing_state, r.device_id, "paired", expect=["unpair_requested"], error=str(exc))
                return
        await self._db(forget_pairing, r.device_id, alias)
        log.info("homekit: %s unpaired", r.device_id)

    # --- loaded pairings --------------------------------------------------------------

    async def reconcile(self) -> None:
        """Make the loaded pairings match the 'paired' rows; inventory + subscribe new ones."""
        rows = await self._db(load_device_rows)
        self._unit_device = await self._db(_unit_devices)
        paired = {r.alias: r for r in rows if r.pairing_state == "paired" and r.alias}
        unpairing = {r.alias for r in rows if r.pairing_state == "unpair_requested" and r.alias}
        for alias in self.bridge.aliases():
            if alias not in paired and alias not in unpairing:
                await self.bridge.unload(alias)
                self._states.pop(alias, None)
        now = self.clock()
        for alias, r in paired.items():
            try:
                await self._ensure_loaded(alias, r, now)
            except Exception as exc:  # noqa: BLE001
                log.warning("homekit %r: could not prepare the pairing: %s", alias, _short(exc))

    async def _ensure_loaded(self, alias: str, r: DeviceRow, now: datetime) -> None:
        if not self.bridge.is_loaded(alias):
            try:
                data = await self._db(get_pairing_secret, alias)
            except secrets.SecretsUnavailable:
                await self._db(set_pairing_error, r.device_id, KEYS_UNREADABLE_MSG)
                return
            if data is None:
                await self._db(set_pairing_state, r.device_id, "failed", expect=["paired"], error=KEYS_MISSING_MSG)
                return
            await self.bridge.load(alias, data)
            self._states[alias] = _AliasState(alias, r.device_id, r.unit_key)
        st = self._states.get(alias)
        if st is None or st.device_id != r.device_id:
            st = self._states[alias] = _AliasState(alias, r.device_id, r.unit_key)
        if st.unit_key != r.unit_key:
            st.unit_key = r.unit_key
            st.ready = False
        if self.bridge.take_config_changed(alias):
            log.info("homekit %r: accessory configuration changed; re-reading the inventory", alias)
            st.ready = False
        if not st.ready and (st.next_inventory is None or now >= st.next_inventory):
            await self._prepare(st, now)

    async def _prepare(self, st: _AliasState, now: datetime) -> None:
        try:
            inventory = await self.bridge.inventory(st.alias)
        except HomekitError as exc:
            st.next_inventory = now + timedelta(seconds=self.poll_s)
            log.info("homekit %r: inventory failed: %s", st.alias, exc)
            res = ReadResult(st.alias, ok=False, available=self.bridge.is_available(st.alias),
                             needs_repair=self.bridge.needs_repair(st.alias), error=str(exc))
            await self._db(record_poll, st.device_id, res, [], now)
            await self._repair_alert(st, res)
            return
        st.aid_map = await self._db(store_inventory, st.device_id, st.unit_key, inventory)
        try:
            pushed = await self.bridge.subscribe(st.alias)
        except HomekitError as exc:
            log.info("homekit %r: subscribe failed: %s", st.alias, exc)
            pushed = 0
        st.ready = True
        st.next_inventory = None
        st.next_poll = None  # poll right away: /accessories values can be stale
        log.info("homekit %r: %d accessories, %d mapped sensors, %d push characteristics",
                 st.alias, len(inventory), len(st.aid_map), pushed)

    async def poll_due(self) -> None:
        now = self.clock()
        due = [
            st for st in self._states.values()
            if st.ready and self.bridge.is_loaded(st.alias)
            and (st.next_poll is None or now >= st.next_poll or st.alias in self._reconnected)
        ]
        if not due:
            return
        for st in due:
            self._reconnected.discard(st.alias)
            if self.bridge.needs_recreate(st.alias):
                await self.bridge.recreate(st.alias)
                with contextlib.suppress(HomekitError):
                    await self.bridge.subscribe(st.alias)
        results = await asyncio.gather(*(self.bridge.read(st.alias) for st in due), return_exceptions=True)
        for st, res in zip(due, results, strict=True):
            st.next_poll = now + timedelta(seconds=self.poll_s)
            if isinstance(res, BaseException):
                log.warning("homekit %r: poll failed: %s", st.alias, _short(res))
                res = ReadResult(st.alias, ok=False, available=self.bridge.is_available(st.alias),
                                 needs_repair=self.bridge.needs_repair(st.alias), error=_short(res))
            elif not res.ok:
                log.info("homekit %r: poll failed (available=%s): %s", st.alias, res.available, res.error)
            readings = readings_from_values(res.values, st.aid_map, now) if res.ok else []
            await self._db(record_poll, st.device_id, res, readings, now)
            await self._repair_alert(st, res)
            if res.ok and st.unit_key:
                await self._fallback_snapshot(st, res, now)

    async def _fallback_snapshot(self, st: _AliasState, res: ReadResult, now: datetime) -> None:
        """Keep the unit's live snapshot fresh from HomeKit while the cloud's is stale, and log
        a person's change at the thermostat while the cloud circuit is open."""
        try:
            done = await self._db(homekit_fallback, st.unit_key, res.values, st.aid_map, now,
                                  self.cloud_stale_after)
            if done.written:
                log.info("homekit %r: cloud snapshot of unit %s is stale; wrote a HomeKit snapshot",
                         st.alias, st.unit_key)
            if done.hand_change_id is not None:
                log.info("homekit %r: someone changed unit %s by hand (action %s); the controller stands aside",
                         st.alias, st.unit_key, done.hand_change_id)
        except Exception as exc:  # noqa: BLE001 - one unit's snapshot never stops the poll
            log.warning("homekit %r: HomeKit snapshot for unit %s failed: %s", st.alias, st.unit_key, _short(exc))

    async def _repair_alert(self, st: _AliasState, res: ReadResult) -> None:
        """Best effort: alert once when the device forgets our pairing; resolve on recovery."""
        key = f"homekit_repair:{st.alias}"
        try:
            if res.needs_repair and not st.repair_alerted:
                await self._db(notify.raise_alert, "homekit_pairing", "error",
                               f"HomeKit pairing lost: {st.alias}", REPAIR_MSG, key)
                st.repair_alerted = True
            elif res.ok and st.repair_alerted:
                await self._db(notify.resolve_alert, key)
                st.repair_alerted = False
        except Exception as exc:  # noqa: BLE001
            log.warning("homekit %r: alert update failed: %s", st.alias, _short(exc))

    async def drain_events(self) -> None:
        if not self._event_aids:
            return
        pending, self._event_aids = self._event_aids, {}
        now = self.clock()
        readings: list[SensorReading] = []
        for alias, aids in pending.items():
            st = self._states.get(alias)
            if st is None or not st.ready or not self.bridge.is_loaded(alias):
                continue
            readings.extend(readings_from_values(self.bridge.values(alias), st.aid_map, now, aids))
        if readings:
            await self._db(ingest_readings, readings)

    # --- queued HomeKit writes --------------------------------------------------------

    def _alias_for_unit(self, unit_key: str) -> str | None:
        device_id = self._unit_device.get(unit_key)
        candidates = [st for st in self._states.values() if st.ready and self.bridge.is_loaded(st.alias)]
        for st in candidates:
            if device_id is not None and st.device_id == device_id:
                return st.alias
        matches = [st.alias for st in candidates if st.unit_key == unit_key]
        return matches[0] if len(matches) == 1 else None

    async def _expire_stuck(self) -> None:
        try:
            await self._db(expire_stuck_actions, self.clock(), self.sent_max_age)
        except Exception as exc:  # noqa: BLE001
            log.warning("homekit: could not expire stuck actions: %s", _short(exc))

    async def execute_actions(self) -> None:
        # No job of this process is in flight here (jobs run inside this call), so any old
        # 'sent' row was left by an earlier run.
        await self._db(expire_stuck_actions, self.clock(), self.sent_max_age)
        jobs = await self._db(claim_queued_actions, self.clock(), self.action_max_age)
        if not jobs:
            return
        tz, limits = await self._db(_tz_and_limits)
        for job in jobs:
            try:
                result = await self._execute(job, tz, limits)
            except Exception as exc:  # noqa: BLE001
                log.warning("homekit: action %s failed unexpectedly: %s", job.id, _short(exc))
                result = WriteResult(ok=False, channel="homekit", request=job.request,
                                     error=f"unexpected error ({_short(exc)})")
            await self._db(finish_action, job.id, result, self.clock())
            log.info("homekit: action %s (%s on %s) -> %s%s", job.id, job.request.get("kind"), job.unit_key,
                     "verified" if result.ok else "failed", "" if result.ok else f": {result.error}")

    async def _execute(self, job: ActionJob, tz: str, limits: HardLimits) -> WriteResult:
        def fail(msg: str) -> WriteResult:
            return WriteResult(ok=False, channel="homekit", request=job.request, error=msg)

        alias = self._alias_for_unit(job.unit_key)
        if alias is None:
            return fail(f"no paired HomeKit thermostat is ready for unit {job.unit_key!r}")
        if not self.bridge.is_available(alias):
            return fail(f"the HomeKit thermostat for unit {job.unit_key!r} is unavailable")
        kind = job.request.get("kind")
        if kind == "climate_hold":
            climate = job.request.get("climate")
            if climate not in CLIMATE_CODES:
                return fail(f"climate must be one of {sorted(CLIMATE_CODES)}, got {climate!r}")
            until = _parse_until(job.request.get("until"))
            lo, hi = hold_window(self.clock(), limits.max_hold_hours)
            if until is None or not (lo <= until <= hi):
                return fail(f"hold end {job.request.get('until')!r} must be 5 min to {limits.max_hold_hours} h ahead")
            # A climate hold holds whatever that comfort setting's targets are: READ them (never
            # written) and refuse a hold the hard limits would not allow as a temperature hold.
            try:
                targets = await self.bridge.read_comfort_targets(alias)
            except HomekitError as exc:
                return fail(f"could not read the comfort settings before the hold ({exc}); not written")
            chosen = targets.get(str(climate), {})
            why = comfort_violation(str(climate), chosen, limits)
            if why is not None:
                return WriteResult(ok=False, channel="homekit", request=job.request, error=why,
                                   before={"climate_targets": {str(climate): chosen}})
            result = await self.bridge.set_climate_hold(alias, climate, until, tz)
            result.before = {**result.before, "climate_targets": {str(climate): chosen}}
            return result
        if kind in CLEAR_HOLD_KINDS:
            return await self.bridge.clear_hold(alias)
        return fail(f"unknown HomeKit request kind {kind!r}")


async def main() -> None:
    from climate.config import get_settings

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # aiohomekit and zeroconf are chatty at DEBUG; keep them at INFO or above.
    for name in ("aiohomekit", "zeroconf"):
        logging.getLogger(name).setLevel(max(logging.INFO, logging.getLogger().level))
    service = HomekitService(HomekitBridge(Path(settings.homekit_state_dir)))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await service.run(stop)


if __name__ == "__main__":
    asyncio.run(main())

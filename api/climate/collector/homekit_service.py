"""Entry point of the host-networked HomeKit service: ``python -m climate.collector.homekit_service``.

Loops: mDNS discovery -> homekit_devices; the pairing handshake driven by the web UI
(pairing_state requested -> start pair-setup -> awaiting_code; code_submitted -> finish ->
save pairing encrypted -> paired; unpair_requested -> remove pairing); for each pairing:
populate accessories, map aids to sensors (by name), subscribe 'ev' characteristics, poll
every 60 s in batches of <= 49, push readings via collector.ingest.ingest_live_readings;
execute queued control_actions with channel 'homekit'; heartbeat 'homekit' every loop.

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

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from climate import events, notify
from climate.collector import ingest
from climate.sources.base import SensorReading, WriteResult
from climate.sources.homekit import (
    BAD_CODE_FORMAT_MSG,
    CLIMATE_CODES,
    CODE_RE,
    NO_PENDING_MSG,
    REPAIR_MSG,
    DiscoveredDevice,
    HomekitBridge,
    HomekitError,
    ReadResult,
    SensorRef,
    build_aid_map,
    hold_window,
    readings_from_values,
)
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
from climate.store.orm import ControlAction, HomekitDevice, Room, Sensor, Unit
from climate.timeutil import utcnow

log = logging.getLogger("climate.homekit")

SECRET_PREFIX = "homekit_pairing:"
SERVICE = "homekit"
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
    """Claim channel='homekit' rows in status 'queued' (-> 'sent'); expire stale ones."""
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
        else:
            r.status = "sent"
            jobs.append(ActionJob(r.id, r.unit_key, r.action, dict(r.request or {})))
        events.publish(session, "action", r.id)
    return jobs


def finish_action(session: Session, action_id: int, result: WriteResult, now: datetime) -> None:
    r = session.get(ControlAction, action_id)
    if r is None:
        return
    r.status = "verified" if result.ok else "failed"
    if r.before is None and result.before:
        r.before = dict(result.before)
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

    async def execute_actions(self) -> None:
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
            return await self.bridge.set_climate_hold(alias, climate, until, tz)
        if kind == "clear_hold":
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

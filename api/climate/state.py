"""Assemble the current ``HouseState`` from the database (live tables, settings, weather,
room states, policy). Used by the controller, the status endpoint and the agent's tools."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from climate.control.policy import (
    HouseState,
    PolicyParams,
    RoomStatus,
    UnitStatus,
    _priority_room,
    _unit_period,
)
from climate.house import ROOMS, UNITS
from climate.occupancy.room_state import (
    RoomStateResult,
    SensorSignal,
    compute_room_states,
    house_empty,
    last_activity,
    load_signals,
)
from climate.sources.base import HoldInfo, UnitSnapshot
from climate.store.app_settings import (
    ControlSettings,
    LocationSettings,
    OccupancySettings,
    get_setting,
)
from climate.store.orm import ControlAction, LiveSensor, LiveUnit, ModelFit, Sensor, WeatherHour
from climate.timeutil import day_bounds_utc, local_date, to_local, utcnow

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=15)
OUTDOOR_MAX_AGE = timedelta(hours=3)  # an observed outdoor temperature older than this is not "now"
PREVIOUS_MAX_GAP = timedelta(minutes=30)  # older room_states rows say nothing about "since"
SINCE_LOOKBACK = timedelta(days=14)
SUNNY_CLOUD_PCT = 40.0
MATCH_F = 0.1  # a hold matches our request within this
MANUAL_KIND = "manual_hold_detected"  # request.kind of the controller's manual-change log rows
RESUME_KIND = "manual_resume_detected"  # request.kind of its "someone cancelled our hold" rows
WRITE_STATUSES = ("verified", "sent")
HOLD_ACTIONS = ("set_hold", "resume_program")  # setpoint writes (program/settings writes are not)
# A queued row is a change on its way (HomeKit fallback, owner actions): it counts toward the
# one-change-per-30-min limit like a sent one.
CHANGE_STATUSES = ("verified", "sent", "queued")
ATTEMPT_STATUSES = ("verified", "sent", "failed")
# ecobee's own automatic events: not a person's change, the controller may override them.
AUTO_EVENTS = ("autoAway", "autoHome")
# Events the controller never overrides (guardrails blocks them): not a manual change either.
PROTECTED_EVENTS = ("vacation", "demandResponse")
# A snapshot must be this much newer than our write before its missing hold says anything: a
# verified write's read-back proved it landed (the grace covers thermostat clock skew); a write
# still in flight ('sent') needs longer (a crashed write).
VERIFIED_GRACE = timedelta(minutes=2)
SENT_GRACE = timedelta(minutes=5)
END_SLACK = timedelta(minutes=5)  # a hold that ends this soon may already have ended on its own
ATTEMPT_WINDOW_SLACK = timedelta(minutes=10)


def load_house_state(session: Session, now: datetime | None = None) -> HouseState:
    """Read live_units/live_sensors, compute room states with
    ``climate.occupancy.room_state.compute_room_states`` (does NOT persist them), attach
    learned room offsets (latest active model_fits kind='room_offsets'), today's forecast
    high/sunniness, the house-empty decision, settings and the active policy version
    (with the running experiment arm's params overlaid, see ``climate.experiments.switchback.active_arm``).
    Missing data never raises: units without a snapshot have snapshot=None, call='unknown'.

    During a change's trial window (afternoons, ``changes.TRIAL_WINDOW``) the trial policy
    version replaces the active one. A hold that matches the controller's own latest write
    (or its latest attempted one whose read-back failed but which landed) is marked
    ``set_by_us``; any other hold a person made starts the manual back-off, and so does our
    hold vanishing early (someone pressed Resume at the thermostat). ecobee's automatic
    events (Smart Away / Home, vacation, demand response) are not a person's change and start
    no back-off; Quick Save is."""
    now = now or utcnow()
    control = _setting(session, "control", ControlSettings)
    occupancy = _setting(session, "occupancy", OccupancySettings)
    tz = _tz(_setting(session, "location", LocationSettings).tz)
    local_now = to_local(now, tz)

    signals = load_signals(session, now)
    previous = previous_room_states(session, now)
    results = compute_room_states(now, tz, signals, occupancy, previous)
    empty, empty_reason = house_empty(now, tz, results, signals, occupancy)

    rooms = _rooms(session, now, signals, results)
    offsets = room_offsets(session)
    for unit in UNITS:
        period = _unit_period(unit.key, rooms, occupancy, local_now, empty)
        prio = _priority_room(unit.key, period, local_now, control.schedule)
        part = "night" if period == "night" else "day"
        for r in rooms.values():
            if r.unit_key != unit.key:
                continue
            r.is_priority = prio is not None and prio[0] == r.room_key
            if r.has_sensor:
                r.offset_f = _offset(offsets.get(r.room_key), part)

    units = _units(session, now, control)
    outdoor, high, sunny = _weather(session, now, tz, units)
    policy, policy_id = _policy(session, now, tz)

    return HouseState(
        now=now,
        tz=tz,
        units=units,
        rooms=rooms,
        outdoor_temp_f=outdoor,
        forecast_high_f=high,
        forecast_sunny=sunny,
        house_empty=empty,
        house_empty_reason=empty_reason,
        control=control,
        occupancy=occupancy,
        policy=policy,
        policy_version_id=policy_id,
    )


def room_results(state: HouseState) -> dict[str, RoomStateResult]:
    """The room states of a HouseState in the shape ``persist_room_states`` takes."""
    return {
        k: RoomStateResult(room_key=k, state=r.state, confidence=r.confidence, reason=r.reason, since=r.since)
        for k, r in state.rooms.items()
    }


# ---------------------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------------------


# One indexed probe per room on the (room_key, ts) primary key: the latest row, the latest
# row with a different state before it, and the first row after that (= since).
_PREVIOUS_SQL = text(
    """
    SELECT rm.key AS room_key, l.state, l.confidence, l.reason, l.ts,
           (SELECT min(r.ts) FROM room_states r
             WHERE r.room_key = rm.key AND r.ts >= :since AND r.ts <= l.ts
               AND (o.ts IS NULL OR r.ts > o.ts)) AS since
    FROM rooms rm
    CROSS JOIN LATERAL (
        SELECT ts, state, confidence, reason FROM room_states
        WHERE room_key = rm.key AND ts >= :since AND ts <= :now
        ORDER BY ts DESC LIMIT 1
    ) l
    LEFT JOIN LATERAL (
        SELECT ts FROM room_states
        WHERE room_key = rm.key AND ts >= :since AND ts < l.ts AND state <> l.state
        ORDER BY ts DESC LIMIT 1
    ) o ON TRUE
    """
)


def previous_room_states(session: Session, now: datetime) -> dict[str, RoomStateResult]:
    """Latest persisted state per room with ``since`` = start of its current run of equal
    states (looking back 14 days). Rooms whose latest row is over 30 min old are left out:
    after a gap we can't say how long a state has lasted."""
    rows = session.execute(_PREVIOUS_SQL, {"since": now - SINCE_LOOKBACK, "now": now}).all()
    out: dict[str, RoomStateResult] = {}
    for room_key, state, confidence, reason, ts, since in rows:
        if now - ts > PREVIOUS_MAX_GAP:
            continue
        out[room_key] = RoomStateResult(room_key, state, float(confidence), reason, since)
    return out


def _rooms(
    session: Session, now: datetime, signals: list[SensorSignal], results: dict[str, RoomStateResult],
) -> dict[str, RoomStatus]:
    live_rows = session.execute(
        select(Sensor.key, Sensor.room_key, LiveSensor.ts, LiveSensor.temp_f, LiveSensor.humidity, LiveSensor.online)
        .outerjoin(LiveSensor, LiveSensor.sensor_key == Sensor.key)
        .where(Sensor.is_active.is_(True))
        .order_by(Sensor.sort, Sensor.key)
    ).all()
    by_room: dict[str, list[Any]] = {}
    for row in live_rows:
        by_room.setdefault(row.room_key, []).append(row)
    sig_by_room: dict[str, list[SensorSignal]] = {}
    for sig in signals:
        sig_by_room.setdefault(sig.room_key, []).append(sig)

    out: dict[str, RoomStatus] = {}
    for room in ROOMS:
        res = results.get(room.key)
        rows = by_room.get(room.key, []) if room.has_sensor else []
        fresh = [r for r in rows if r.ts is not None and r.online and now - r.ts <= STALE_AFTER]
        temps = [float(r.temp_f) for r in fresh if r.temp_f is not None]
        hums = [float(r.humidity) for r in fresh if r.humidity is not None]
        newest = max((r.ts for r in rows if r.ts is not None), default=None)
        stale = room.has_sensor and (newest is None or now - newest > STALE_AFTER)
        lasts = [t for t in (last_activity(s, now) for s in sig_by_room.get(room.key, []) if s.has_occupancy) if t]
        out[room.key] = RoomStatus(
            room_key=room.key,
            name=room.name,
            unit_key=room.unit_key,
            floor=room.floor,
            has_sensor=room.has_sensor,
            is_sleep_room=room.is_sleep_room,
            has_comfort_target=room.has_comfort_target,
            temp_f=round(sum(temps) / len(temps), 2) if temps else None,
            humidity=round(sum(hums) / len(hums), 1) if hums else None,
            state=res.state if res else "unknown",  # type: ignore[arg-type]
            confidence=res.confidence if res else 0.0,
            reason=res.reason if res else "No occupancy result for this room.",
            since=res.since if res else None,
            sensor_keys=[r.key for r in rows] if room.has_sensor else [],
            seconds_since_motion=int((now - max(lasts)).total_seconds()) if lasts else None,
            stale=stale,
        )
    return out


def room_offsets(session: Session) -> dict[str, Any]:
    """Merged ``offsets`` of the active 'room_offsets' fits (newest wins per room)."""
    rows = session.execute(
        select(ModelFit.params)
        .where(ModelFit.kind == "room_offsets", ModelFit.status == "active")
        .order_by(ModelFit.created_at, ModelFit.id)
    ).scalars()
    merged: dict[str, Any] = {}
    for params in rows:
        offsets = params.get("offsets") if isinstance(params, dict) else None
        if isinstance(offsets, dict):
            merged.update(offsets)
    return merged


def _offset(raw: Any, part: str) -> float | None:
    """Read one room's offset defensively: {"day": f, "night": f} or a bare number."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        value = raw.get(part, raw.get("all"))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


# ---------------------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------------------


def _units(session: Session, now: datetime, control: ControlSettings) -> dict[str, UnitStatus]:
    live = {row.unit_key: row for row in session.execute(select(LiveUnit)).scalars()}
    last_change = dict(
        session.execute(
            select(ControlAction.unit_key, func.max(ControlAction.ts))
            .where(ControlAction.status.in_(CHANGE_STATUSES), ControlAction.actor.in_(("controller", "owner")),
                   ControlAction.action.in_(HOLD_ACTIONS))
            .group_by(ControlAction.unit_key)
        ).all()
    )
    out: dict[str, UnitStatus] = {}
    for unit in UNITS:
        row = live.get(unit.key)
        snap: UnitSnapshot | None = None
        if row is not None:
            try:
                snap = UnitSnapshot.model_validate(row.snapshot)
            except ValidationError:
                log.warning("live_units snapshot for %s does not parse; treating the unit as unknown", unit.key)
        # the newest snapshot, whichever source wrote it (HomeKit takes over while the cloud is down)
        stamps = [t for t in (snap.ts if snap is not None else None, row.ts if row is not None else None) if t]
        ref_ts = max(stamps) if stamps else None
        status = UnitStatus(
            unit_key=unit.key,
            name=unit.name,
            snapshot=snap,
            age_s=max(0.0, (now - ref_ts).total_seconds()) if ref_ts is not None else None,
            call=_call(snap),
            last_change_at=last_change.get(unit.key),
        )
        if snap is not None:
            status.manual_override_until = _manual_override(session, unit.key, snap, control, now)
        out[unit.key] = status
    return out


def _call(snap: UnitSnapshot | None) -> str:
    if snap is None:
        return "unknown"
    running = [e.lower() for e in snap.equipment_running]
    if any(e.startswith("compcool") for e in running):
        return "cool"
    if any(e.startswith(("compheat", "auxheat", "heatpump")) for e in running):
        return "heat"
    if "fan" in running:
        return "fan"
    return "idle"


def latest_write(session: Session, unit_key: str) -> ControlAction | None:
    """Our newest thermostat write for the unit that went out (verified or sent)."""
    return session.execute(
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.status.in_(WRITE_STATUSES),
            ControlAction.action.in_(HOLD_ACTIONS),
        )
        .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_attempt(session: Session, unit_key: str) -> ControlAction | None:
    """The controller's newest hold write or resume for the unit that was sent to the
    thermostat, including one whose read-back failed (it may have landed anyway)."""
    return session.execute(
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.actor == "controller",
            ControlAction.status.in_(ATTEMPT_STATUSES),
            ControlAction.action.in_(HOLD_ACTIONS),
            ControlAction.channel != "none",
        )
        .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def write_end(write: ControlAction, default_hours: float) -> datetime | None:
    """When the hold a set_hold row wrote ends: its HomeKit ``until``, else (completed_at or
    ts) + its hours."""
    req = write.request if isinstance(write.request, dict) else {}
    until = req.get("until")
    if isinstance(until, str):
        try:
            end = datetime.fromisoformat(until)
            return end if end.tzinfo is not None else None
        except ValueError:
            return None
    hours = req.get("hours", default_hours)
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        hours = default_hours
    return (write.completed_at or write.ts) + timedelta(hours=float(hours))


def cancelled_write(
    session: Session, unit_key: str, snap: UnitSnapshot | None, control: ControlSettings, now: datetime,
) -> ControlAction | None:
    """Our latest set_hold for the unit when a snapshot NEWER than it shows no hold although
    that hold should still be running: someone pressed Resume at the thermostat (or cancelled
    our hold in the ecobee app). None otherwise.

    Snapshots that came over HomeKit say nothing here (HomeKit shows no ecobee holds). A
    write still in flight ('sent') needs a snapshot 5 minutes newer; a newer failed attempt
    makes the picture ambiguous, so it says nothing either. A hold that was due to end within
    5 minutes of the snapshot may simply have run out."""
    if snap is None or snap.hold is not None or snap.source == "homekit":
        return None
    write = latest_write(session, unit_key)
    if write is None or write.action != "set_hold":
        return None
    attempt = latest_attempt(session, unit_key)
    if attempt is not None and attempt.status == "failed" and (attempt.ts, attempt.id) > (write.ts, write.id):
        return None
    if write.status == "verified" and write.completed_at is not None:
        seen_from = write.completed_at + VERIFIED_GRACE
    else:
        seen_from = write.ts + SENT_GRACE
    if snap.ts <= seen_from:
        return None
    end = write_end(write, control.hold_hours)
    if end is None or now >= end or snap.ts >= end - END_SLACK:
        return None
    return write


def resume_detection(session: Session, unit_key: str, after: datetime) -> ControlAction | None:
    """The controller's log row for "our hold was cancelled at the thermostat", after ``after``."""
    return session.execute(
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == RESUME_KIND,
            ControlAction.ts > after,
        )
        .order_by(ControlAction.ts, ControlAction.id)
        .limit(1)
    ).scalar_one_or_none()


def _in_attempt_window(hold: HoldInfo, attempt: ControlAction, default_hours: float) -> bool:
    """The hold could be what ``attempt`` wrote: it started no earlier than the attempt and
    ends within the attempt's hold hours (plus read-back slack)."""
    if hold.end is None:
        return False
    req = attempt.request if isinstance(attempt.request, dict) else {}
    hours = req.get("hours", default_hours)
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        hours = default_hours
    sent = attempt.ts
    if hold.start is not None and hold.start < sent - ATTEMPT_WINDOW_SLACK:
        return False
    return sent - ATTEMPT_WINDOW_SLACK <= hold.end <= sent + timedelta(hours=float(hours)) + ATTEMPT_WINDOW_SLACK


def hold_matches(hold: HoldInfo, request: dict[str, Any] | None) -> bool:
    """True when the thermostat's hold is what ``request`` asked for (within 0.1°F)."""
    if not isinstance(request, dict):
        return False
    if request.get("kind") == "climate_hold":
        climate = request.get("climate")
        return bool(climate) and (hold.climate_ref or "").lower() == str(climate).lower()
    pairs = [(hold.heat_f, request.get("heat_f")), (hold.cool_f, request.get("cool_f"))]
    seen = [(h, r) for h, r in pairs if h is not None]
    if not seen:
        return False
    for held, asked in seen:
        if not isinstance(asked, (int, float)) or isinstance(asked, bool) or abs(held - float(asked)) > MATCH_F:
            return False
    return True


def hold_signature(hold: HoldInfo) -> dict[str, Any]:
    return {
        "kind": MANUAL_KIND,
        "heat_f": hold.heat_f,
        "cool_f": hold.cool_f,
        "climate_ref": hold.climate_ref,
        "hold_type": hold.hold_type,
    }


def same_signature(a: dict[str, Any] | None, b: dict[str, Any]) -> bool:
    if not isinstance(a, dict) or a.get("kind") != MANUAL_KIND:
        return False
    for key in ("heat_f", "cool_f"):
        x, y = a.get(key), b.get(key)
        if (x is None) != (y is None):
            return False
        if x is not None and abs(float(x) - float(y)) > MATCH_F:
            return False
    return (a.get("climate_ref") or None) == (b.get("climate_ref") or None)


def manual_detection(session: Session, unit_key: str, hold: HoldInfo, after: datetime | None) -> ControlAction | None:
    """The controller's first log row for this manual hold, written after our last write
    (rows with ``refused`` are resumes the source refused because the hold was not ours)."""
    q = (
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == MANUAL_KIND,
        )
        .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
        .limit(50)
    )
    if after is not None:
        q = q.where(ControlAction.ts > after)
    sig = hold_signature(hold)
    rows = [r for r in session.execute(q).scalars() if same_signature(r.request, sig)]
    return rows[-1] if rows else None


def _manual_override(
    session: Session, unit_key: str, snap: UnitSnapshot, control: ControlSettings, now: datetime,
) -> datetime | None:
    """When the manual back-off for this unit ends, or None when nobody changed it by hand."""
    backoff = timedelta(hours=control.manual_backoff_hours)
    hold = snap.hold
    if hold is None:
        # our hold vanished before its end: someone resumed the schedule at the thermostat
        cancelled = cancelled_write(session, unit_key, snap, control, now)
        if cancelled is None:
            return None
        seen = resume_detection(session, unit_key, cancelled.ts)
        return (seen.ts if seen is not None else now) + backoff  # first seen (the tick logs it now)
    if hold.hold_type in AUTO_EVENTS or hold.hold_type in PROTECTED_EVENTS:
        hold.set_by_us = False
        return None  # ecobee's own event, not a person (guardrails blocks vacation / DR)
    write = latest_write(session, unit_key)
    # Already logged as a person's hold after our last write (including a resume the source
    # refused because the hold was not ours): that decision stands.
    seen = manual_detection(session, unit_key, hold, write.ts if write is not None else None)
    if seen is not None:
        hold.set_by_us = False
        refused = isinstance(seen.request, dict) and bool(seen.request.get("refused"))
        return (seen.ts if refused or hold.start is None else hold.start) + backoff
    if write is not None and write.action == "set_hold" and hold_matches(hold, write.request):
        if write.actor == "owner":
            return (write.completed_at or write.ts) + backoff  # the owner's own hold from the app
        hold.set_by_us = True
        return None
    attempt = latest_attempt(session, unit_key)
    if (
        attempt is not None and attempt.action == "set_hold" and (write is None or attempt.id != write.id)
        and hold_matches(hold, attempt.request) and _in_attempt_window(hold, attempt, control.hold_hours)
    ):
        hold.set_by_us = True  # our write landed although its read-back failed
        return None
    if hold.set_by_us:
        return None  # the source itself recognised the hold as ours
    # first seen: right now (the tick logs it now)
    return (hold.start if hold.start is not None else now) + backoff


# ---------------------------------------------------------------------------------------
# weather, policy, settings
# ---------------------------------------------------------------------------------------


def _weather(
    session: Session, now: datetime, tz: str, units: dict[str, UnitStatus],
) -> tuple[float | None, float | None, bool | None]:
    outdoor = session.execute(
        select(WeatherHour.temp_f)
        .where(
            WeatherHour.kind == "observed", WeatherHour.temp_f.is_not(None),
            WeatherHour.ts <= now, WeatherHour.ts >= now - OUTDOOR_MAX_AGE,
        )
        .order_by(WeatherHour.ts.desc(), WeatherHour.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if outdoor is None:
        for key in ("main", "up", "bed"):
            snap = units[key].snapshot if key in units else None
            if snap is not None and snap.outdoor_temp_f is not None:
                outdoor = snap.outdoor_temp_f
                break

    day = local_date(now, tz)
    start, end = day_bounds_utc(day, tz)
    high = session.execute(
        select(func.max(WeatherHour.temp_f)).where(WeatherHour.ts >= start, WeatherHour.ts < end)
    ).scalar_one_or_none()
    noon = datetime.combine(day, datetime.min.time(), ZoneInfo(tz)).replace(hour=12)
    afternoon_start, afternoon_end = noon, noon + timedelta(hours=6)
    clouds = session.execute(
        select(func.avg(WeatherHour.cloud_cover)).where(
            WeatherHour.ts >= afternoon_start, WeatherHour.ts < afternoon_end, WeatherHour.cloud_cover.is_not(None)
        )
    ).scalar_one_or_none()
    sunny = None if clouds is None else float(clouds) < SUNNY_CLOUD_PCT
    return (
        float(outdoor) if outdoor is not None else None,
        float(high) if high is not None else None,
        sunny,
    )


def _policy(session: Session, now: datetime, tz: str) -> tuple[PolicyParams, int | None]:
    from climate.control import changes

    try:
        with session.begin_nested():
            params, policy_id = changes.policy_for(session, now, tz)
    except Exception:  # state must never fail on the policy lookup
        log.warning("could not load the policy; using the defaults", exc_info=True)
        params, policy_id = PolicyParams(), None

    try:
        with session.begin_nested():
            from climate.experiments import switchback

            arm = switchback.active_arm(session, now, tz)
    except NotImplementedError:
        arm = None
    except Exception:  # an unfinished or failing experiment module never breaks state
        log.warning("experiments.switchback.active_arm failed; running without the experiment arm", exc_info=True)
        arm = None
    if arm:
        _, arm_params = arm
        if isinstance(arm_params, dict) and arm_params:
            known = {k: v for k, v in arm_params.items() if k in PolicyParams.model_fields}
            try:
                params = PolicyParams.model_validate({**params.model_dump(), **known})
            except ValidationError:
                log.warning("experiment arm params are invalid; ignoring them: %s", arm_params)
    return params, policy_id



def _setting(session: Session, key: str, model: type[BaseModel]) -> Any:
    try:
        return get_setting(session, key, model)
    except ValidationError:
        log.warning("app_settings[%s] does not parse; using the defaults", key)
        return model()


def _tz(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return tz

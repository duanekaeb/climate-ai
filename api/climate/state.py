"""Assemble the current ``HouseState`` from the database (live tables, settings, weather,
room states, policy, utility events). Used by the controller, the status endpoint and the
agent's tools."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, aliased

from climate.control.policy import (
    HouseState,
    PersonHold,
    PolicyParams,
    RoomStatus,
    UnitStatus,
    UtilityEventState,
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
from climate.sources.base import (
    AUTO_EVENTS,
    KNOWN_HOLD_TYPES,
    PERSON_EVENTS,
    PROTECTED_EVENTS,
    HoldInfo,
    UnitSnapshot,
)
from climate.store.app_settings import (
    ControlSettings,
    LocationSettings,
    OccupancySettings,
    UtilityEventSettings,
    get_setting,
)
from climate.store.orm import ControlAction, LiveSensor, LiveUnit, ModelFit, Sensor, WeatherHour
from climate.timeutil import day_bounds_utc, local_date, to_local, utcnow

log = logging.getLogger(__name__)

# AUTO_EVENTS (ecobee's own Smart Away / Home: not a person, the controller may take the unit
# back) and PROTECTED_EVENTS (vacation, demand response: never overridden) live in
# climate.sources.base; other modules import them from here too.

STALE_AFTER = timedelta(minutes=15)
OUTDOOR_MAX_AGE = timedelta(hours=3)  # an observed outdoor temperature older than this is not "now"
PREVIOUS_MAX_GAP = timedelta(minutes=30)  # older room_states rows say nothing about "since"
SINCE_LOOKBACK = timedelta(days=14)
SUNNY_CLOUD_PCT = 40.0
MATCH_F = 0.1  # a hold matches a write within this
# request.kind of the detection row logged once per person hold (by the controller, or by the
# homekit service with request.source 'homekit' while the ecobee cloud is down) ...
MANUAL_KIND = "manual_hold_detected"
# ... and of the row logged once per Resume someone pressed on a running hold.
RESUME_KIND = "manual_resume_detected"
HOMEKIT_SOURCE = "homekit"  # request.source of the homekit service's detection rows
# request.kind of the owner's two ways to end a hold from this app (owner resume_program rows):
OWNER_AUTOMATIC = "automatic"  # "Back to automatic": the controller steers again at once
OWNER_RESUME_SCHEDULE = "resume_schedule"  # "Resume schedule": the ecobee schedule runs for a while
WRITE_STATUSES = ("verified", "sent")
HOLD_ACTIONS = ("set_hold", "resume_program")  # setpoint writes (program/settings writes are not)
# A queued row is a change on its way (HomeKit fallback, owner actions): it counts toward the
# one-change-per-30-min limit like a sent one.
CHANGE_STATUSES = ("verified", "sent", "queued")
ATTEMPT_STATUSES = ("verified", "sent", "failed")
# A snapshot must be this much newer than our write before its missing hold says anything: a
# verified write's read-back proved it landed (the grace covers thermostat clock skew); a write
# still in flight ('sent') needs longer (a crashed write).
VERIFIED_GRACE = timedelta(minutes=2)
SENT_GRACE = timedelta(minutes=5)
END_SLACK = timedelta(minutes=5)  # a hold that ends this soon may already have ended on its own
WINDOW_SLACK = timedelta(minutes=10)  # a hold is a write's only if it starts/ends within this of it
INDEFINITE_YEAR = 2035  # ecobee reports an "until I change it" hold as ending in 2035 or later
SAME_START = timedelta(seconds=60)  # two sightings of one hold report the same start


def load_house_state(session: Session, now: datetime | None = None) -> HouseState:
    """Read live_units/live_sensors, compute room states with
    ``climate.occupancy.room_state.compute_room_states`` (does NOT persist them), attach
    learned room offsets (latest active model_fits kind='room_offsets'), today's forecast
    high/sunniness, the house-empty decision, settings, the active policy version (with the
    running experiment arm's params overlaid, see ``climate.experiments.switchback.active_arm``)
    and the open utility events (``climate.utility.events.load_active``).
    Missing data never raises: units without a snapshot have snapshot=None, call='unknown'.

    During a change's trial window (afternoons, ``changes.TRIAL_WINDOW``) the trial policy
    version replaces the active one.

    Holds (see ``person_hold`` and ``resume_backoff``): a running hold that matches the
    controller's own latest write (setpoints AND window) is marked ``set_by_us``; any other
    plain hold or Quick Save is a person's hold (``UnitStatus.person_hold``), which wins for as
    long as it runs. A Resume someone pressed on a running hold (ours, or a person's before it
    was due to end) starts the resume back-off (``resume_backoff_until``). ecobee's own events
    (Smart Away / Home, vacation, demand response, unknown types) are neither."""
    now = now or utcnow()
    control = _setting(session, "control", ControlSettings)
    occupancy = _setting(session, "occupancy", OccupancySettings)
    utility = _setting(session, "utility_events", UtilityEventSettings)
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
        utility_events=_utility_events(session, now),
        utility=utility,
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
    # The owner's "Back to automatic" (and "Resume schedule" while no wait follows a Resume) is
    # not a change of setpoints: it never delays the controller's next write, and neither does
    # anything the owner did on that unit before it (their hold or Resume schedule, which it
    # cancelled). The controller's own writes always count; an owner hold not followed by one
    # counts too (until the snapshot shows it, the limit is what stops a write decided from the
    # old snapshot).
    at_once = (OWNER_AUTOMATIC, OWNER_RESUME_SCHEDULE) if control.resume_backoff_hours <= 0 else (OWNER_AUTOMATIC,)
    auto = aliased(ControlAction)
    cleared = (
        select(auto.id)
        .where(auto.unit_key == ControlAction.unit_key, auto.actor == "owner", auto.action == "resume_program",
               auto.status == "verified", auto.request["kind"].astext.in_(at_once), auto.ts >= ControlAction.ts)
        .exists()
    )
    last_change = dict(
        session.execute(
            select(ControlAction.unit_key, func.max(ControlAction.ts))
            .where(ControlAction.status.in_(CHANGE_STATUSES), ControlAction.actor.in_(("controller", "owner")),
                   ControlAction.action.in_(HOLD_ACTIONS),
                   func.coalesce(ControlAction.request["kind"].astext, "") != OWNER_AUTOMATIC,
                   or_(ControlAction.actor == "controller", ~cleared))
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
        pending = False
        if snap is not None:
            status.person_hold = person_hold(session, unit.key, snap, control, now)
            pending = status.person_hold is None and pending_resume(session, unit.key, snap, control, now) is not None
        backoff = resume_backoff(session, unit.key, control, now, pending=pending)
        if backoff is not None:
            status.resume_backoff_until, status.resume_seen_at = backoff
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


def _utility_events(session: Session, now: datetime) -> list[UtilityEventState]:
    """Announced and running utility events (``climate.utility.events.load_active``); empty
    when they cannot be read, so the house state always loads."""
    try:
        with session.begin_nested():
            from climate.utility.events import load_active

            return load_active(session, now)
    except Exception:  # an unreadable events table never breaks the house state
        log.warning("could not load utility events; planning without them", exc_info=True)
        return []


# ---------------------------------------------------------------------------------------
# holds: ours, a person's, or a Resume
# ---------------------------------------------------------------------------------------


def latest_write(session: Session, unit_key: str, actor: str | None = None) -> ControlAction | None:
    """The newest thermostat hold write or resume for the unit that went out (verified or
    sent), by anyone (or only by ``actor``)."""
    q = select(ControlAction).where(
        ControlAction.unit_key == unit_key,
        ControlAction.status.in_(WRITE_STATUSES),
        ControlAction.action.in_(HOLD_ACTIONS),
    )
    if actor is not None:
        q = q.where(ControlAction.actor == actor)
    return session.execute(q.order_by(ControlAction.ts.desc(), ControlAction.id.desc()).limit(1)).scalar_one_or_none()


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


def latest_detection(session: Session, unit_key: str) -> ControlAction | None:
    """The newest person-hold detection row for the unit (the controller's, or the homekit
    service's while the cloud is down; refused resumes are detections too)."""
    return session.execute(
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == MANUAL_KIND,
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
        end = _parse_dt(until)
        return end if end is not None and end.tzinfo is not None else None
    return (write.completed_at or write.ts) + timedelta(hours=_hours(req, default_hours))


def cancelled_write(
    session: Session, unit_key: str, snap: UnitSnapshot | None, control: ControlSettings, now: datetime,
) -> ControlAction | None:
    """The controller's latest set_hold for the unit when a snapshot NEWER than it shows no
    hold although that hold should still be running: someone pressed Resume at the thermostat
    (or cancelled our hold in the ecobee app). None otherwise.

    Snapshots that came over HomeKit say nothing here (HomeKit shows no ecobee holds). A
    write still in flight ('sent') needs a snapshot 5 minutes newer; a newer failed attempt
    makes the picture ambiguous, so it says nothing either; so does a person's hold seen after
    our write (it replaced ours). A hold that was due to end within 5 minutes of the snapshot
    may simply have run out."""
    if snap is None or snap.hold is not None or snap.source == "homekit":
        return None
    write = latest_write(session, unit_key)
    if write is None or write.action != "set_hold" or write.actor != "controller":
        return None
    attempt = latest_attempt(session, unit_key)
    if attempt is not None and attempt.status == "failed" and (attempt.ts, attempt.id) > (write.ts, write.id):
        return None
    seen = latest_detection(session, unit_key)
    if seen is not None and (seen.ts, seen.id) > (write.ts, write.id):
        return None
    if snapshot_predates(snap, write):
        return None
    end = write_end(write, control.hold_hours)
    if end is None or now >= end or snap.ts >= end - END_SLACK:
        return None
    return write


def snapshot_predates(snap: UnitSnapshot | None, write: ControlAction) -> bool:
    """The snapshot was fetched before ``write`` settled, so it cannot show what the write did:
    not newer than a verified write's completion + 2 min (the read-back proved it landed; the
    grace covers clock skew), or than any other write's queue time + 5 min (in flight, failed,
    or crashed). No snapshot predates everything."""
    if snap is None:
        return True
    if write.status == "verified" and write.completed_at is not None:
        settled = write.completed_at + VERIFIED_GRACE
    else:
        settled = write.ts + SENT_GRACE
    return snap.ts <= settled


def resume_detection(session: Session, unit_key: str, after: datetime) -> ControlAction | None:
    """The controller's first "someone pressed Resume" row for the unit after ``after``."""
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


def resume_logged_for(session: Session, detection: ControlAction) -> ControlAction | None:
    """The "someone pressed Resume" row logged for the person's hold of ``detection``."""
    return session.execute(
        select(ControlAction)
        .where(
            ControlAction.unit_key == detection.unit_key,
            ControlAction.ts >= detection.ts,
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == RESUME_KIND,
            ControlAction.request["person_hold_detection_id"].astext == str(detection.id),
        )
        .limit(1)
    ).scalar_one_or_none()


def is_indefinite(hold: HoldInfo) -> bool:
    """An "until I change it" hold (ecobee reports its end in 2035 or later)."""
    return hold.hold_type == "indefinite" or (hold.end is not None and hold.end.year >= INDEFINITE_YEAR)


def person_until(hold: HoldInfo) -> datetime | None:
    """When a person's hold ends on its own; None when it runs until someone changes it."""
    if is_indefinite(hold) or hold.end is None:
        return None
    return hold.end


def in_write_window(hold: HoldInfo, write: ControlAction, default_hours: float) -> bool:
    """The hold could be what ``write`` put on the thermostat: it is not indefinite, it started
    no earlier than 10 minutes before the write, and it ends within [write - 10 min, write +
    its hours (or its HomeKit ``until``) + 10 min]. A hold at our setpoints with any other
    window is somebody else's (bug 3)."""
    if is_indefinite(hold) or hold.end is None:
        return False
    sent = write.ts
    if hold.start is not None and hold.start < sent - WINDOW_SLACK:
        return False
    req = write.request if isinstance(write.request, dict) else {}
    until = _parse_dt(req.get("until"))
    latest_end = until if until is not None else (write.completed_at or sent) + timedelta(hours=_hours(req, default_hours))
    return sent - WINDOW_SLACK <= hold.end <= latest_end + WINDOW_SLACK


def hold_matches(hold: HoldInfo, request: dict[str, Any] | None) -> bool:
    """True when the thermostat's hold has the setpoints ``request`` asked for (within 0.1°F),
    or its comfort setting for a HomeKit climate hold."""
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


def hold_signature(hold: HoldInfo, *, by: str = "thermostat") -> dict[str, Any]:
    """The request of a person-hold detection row: enough to recognise the same hold on later
    snapshots (setpoints, comfort setting, start) and to tell later whether it ended on its own
    (``until``; None = until someone changes it)."""
    until = person_until(hold)
    return {
        "kind": MANUAL_KIND,
        "by": by,
        "heat_f": hold.heat_f,
        "cool_f": hold.cool_f,
        "climate_ref": hold.climate_ref,
        "hold_type": hold.hold_type,
        "start": hold.start.isoformat() if hold.start is not None else None,
        "end": hold.end.isoformat() if hold.end is not None else None,
        "until": until.isoformat() if until is not None else None,
    }


def same_signature(a: dict[str, Any] | None, b: dict[str, Any]) -> bool:
    """Is detection request ``a`` the same hold as signature ``b``? Same setpoints and comfort
    setting, and the same start when both know it (two identical holds set one after the
    other are two holds)."""
    if not isinstance(a, dict) or a.get("kind") != MANUAL_KIND:
        return False
    for key in ("heat_f", "cool_f"):
        x, y = a.get(key), b.get(key)
        if (x is None) != (y is None):
            return False
        if x is not None and abs(float(x) - float(y)) > MATCH_F:
            return False
    if (a.get("climate_ref") or None) != (b.get("climate_ref") or None):
        return False
    sa, sb = _parse_dt(a.get("start")), _parse_dt(b.get("start"))
    return sa is None or sb is None or abs(sa - sb) <= SAME_START


def manual_detection(session: Session, unit_key: str, hold: HoldInfo, after: datetime | None) -> ControlAction | None:
    """The controller's detection row for this person's hold, logged after the last write to
    the unit (rows with ``refused`` are writes the source refused because the hold was not
    ours). The homekit service's rows describe what HomeKit saw, not an ecobee hold: never.
    Neither is a row in the previous release's format (no ``until``): it cannot tell when its
    hold ends or whether it was resumed, so the first cloud sighting logs the hold again in
    the current format (``_carried_person_hold`` and ``vanished_person_hold`` can then read it)."""
    q = (
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == MANUAL_KIND,
            func.coalesce(ControlAction.request["source"].astext, "") != HOMEKIT_SOURCE,
        )
        .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
        .limit(50)
    )
    if after is not None:
        q = q.where(ControlAction.ts > after)
    sig = hold_signature(hold)
    rows = [r for r in session.execute(q).scalars()
            if isinstance(r.request, dict) and "until" in r.request and same_signature(r.request, sig)]
    return rows[-1] if rows else None


HoldOwner = Literal["ours", "app", "person"]


def hold_owner(session: Session, unit_key: str, hold: HoldInfo, latest: ControlAction | None,
               control: ControlSettings) -> HoldOwner:
    """Whose plain hold this is (bug 3).

    'app': the owner's latest hold from this app's hold form (setpoints and window match).
    'ours': the controller's latest write (verified/sent), or its newer attempt whose
    read-back failed but which landed, matches on setpoints AND window; or the source itself
    recognises it as ours (the ecobee adapter's record of our last hold, end within 5 min),
    unless the owner wrote a hold after us (that record would be the owner's hold).
    'person': anything else, including our setpoints with any other window."""
    hours = control.hold_hours
    owner_last = latest is not None and latest.actor == "owner" and latest.action == "set_hold"
    if owner_last and hold_matches(hold, latest.request) and in_write_window(hold, latest, hours):  # type: ignore[union-attr]
        return "app"
    ctrl = latest if latest is not None and latest.actor == "controller" else latest_write(session, unit_key, "controller")
    if ctrl is not None and ctrl.action == "set_hold" and hold_matches(hold, ctrl.request) \
            and in_write_window(hold, ctrl, hours):
        return "ours"
    attempt = latest_attempt(session, unit_key)
    if (
        attempt is not None and attempt.action == "set_hold" and attempt.status == "failed"
        and (ctrl is None or (attempt.ts, attempt.id) > (ctrl.ts, ctrl.id))
        and hold_matches(hold, attempt.request) and in_write_window(hold, attempt, hours)
    ):
        return "ours"  # our write landed although its read-back failed
    if hold.set_by_us and not owner_last and not is_indefinite(hold):
        return "ours"
    return "person"


def person_hold(
    session: Session, unit_key: str, snap: UnitSnapshot, control: ControlSettings, now: datetime,
) -> PersonHold | None:
    """The person's hold running on the unit, or None. Also settles ``snap.hold.set_by_us``.

    A cloud snapshot's plain hold or Quick Save that is not ours (``hold_owner``) is a
    person's hold: ``since`` = its start, else when first seen; ``until`` = its end, None for
    "until I change it"; ``detection_id`` = the detection row already logged for it (None on
    first sight: the controller logs it this tick). A timed hold whose end has passed by the
    clock is over even while the snapshot still shows it. ecobee's own events are never a
    person's hold.

    A snapshot fetched before the latest write to the unit settled (``snapshot_predates``:
    e.g. the tick runs before the poll after Back to automatic, Resume schedule or an owner's
    hold) cannot start a NEW person's hold seen at the thermostat: that write may have just
    replaced it. It is None until a newer snapshot decides; the hold is not marked ours either.
    The rate limit (and the source's own check of what runs before it writes) keeps the
    controller off it meanwhile. The other way round, when the source refused a controller
    write because of a person's hold (a detection row with ``refused``, from the source's own
    fresh read) and this snapshot is not newer than that refusal, the refusal's row is the
    person's hold (``_carried_person_hold``): the old snapshot cannot show it yet.

    A snapshot from HomeKit shows no ecobee holds and never starts or ends one: the newest
    detection row since the last write stands (``_carried_person_hold``). That is the homekit
    service's row for a change it saw by hand during this HomeKit run (until None, while the
    snapshot source stays 'homekit'), or the controller's row for a person's hold seen on the
    cloud before, while it has not ended or been resumed."""
    latest = latest_write(session, unit_key)
    if snap.source == "homekit":
        return _carried_person_hold(session, unit_key, latest, now, snap)
    if _refused_after(session, unit_key, snap, latest):
        # the source refused a write because of a person's hold this snapshot is too old to show
        carried = _carried_person_hold(session, unit_key, latest, now)
        if carried is not None:
            if snap.hold is not None:
                snap.hold.set_by_us = False
            return carried
    hold = snap.hold
    if hold is None:
        return None
    if hold.hold_type in AUTO_EVENTS or hold.hold_type in PROTECTED_EVENTS or (
        hold.hold_type is not None and hold.hold_type not in KNOWN_HOLD_TYPES
    ):
        hold.set_by_us = False  # ecobee's own event, not a person (guardrails blocks protected/unknown ones)
        return None
    # Already logged as a person's hold after the last write (including a resume the source
    # refused because the hold was not ours): that decision stands.
    seen = manual_detection(session, unit_key, hold, latest.ts if latest is not None else None)
    by = "thermostat"
    if seen is None and hold.hold_type not in PERSON_EVENTS:
        owner = hold_owner(session, unit_key, hold, latest, control)
        if owner == "ours":
            hold.set_by_us = True
            return None
        by = "app" if owner == "app" else "thermostat"
    hold.set_by_us = False
    if seen is None and by == "thermostat" and latest is not None and snapshot_predates(snap, latest):
        return None  # a snapshot from before that write settled: the next one decides
    until = person_until(hold)
    if until is not None and until <= now:
        return None  # it ended on its own; the next snapshot drops it
    req = seen.request if seen is not None and isinstance(seen.request, dict) else {}
    first_seen = seen.ts if seen is not None else now
    return PersonHold(
        since=hold.start or first_seen, first_seen=first_seen,
        by="app" if req.get("by", by) == "app" else "thermostat",
        hold_type=hold.hold_type, until=until, heat_f=hold.heat_f, cool_f=hold.cool_f,
        climate_ref=hold.climate_ref, detection_id=seen.id if seen is not None else None,
    )


def _refused_after(session: Session, unit_key: str, snap: UnitSnapshot, latest: ControlAction | None) -> bool:
    """The newest detection row is a write the source refused over a person's hold (after the
    last write), and the snapshot was fetched before that refusal."""
    det = latest_detection(session, unit_key)
    if det is None or not isinstance(det.request, dict) or not det.request.get("refused"):
        return False
    if det.request.get("source") == HOMEKIT_SOURCE:
        return False
    if latest is not None and (latest.ts, latest.id) >= (det.ts, det.id):
        return False
    return snap.ts <= (det.completed_at or det.ts)


def _carried_person_hold(
    session: Session, unit_key: str, latest: ControlAction | None, now: datetime, snap: UnitSnapshot | None = None,
) -> PersonHold | None:
    """The person's hold a detection row stands for while the snapshot cannot show it (a
    HomeKit snapshot, or a cloud one older than a refused write): the newest detection row
    logged after the last write, read as follows.

    - The homekit service's row (a change seen by hand): a person's hold until the cloud
      returns, but only for the HomeKit run it was seen in. A row older than the snapshot's
      ``settings['homekit_since']`` (when HomeKit took over this unit's live snapshot) belongs
      to an earlier outage: the cloud has reported since, so it is skipped (the next newer
      detection decides). Without that key every HomeKit row counts.
    - The controller's row (a hold seen on the cloud): while it has not ended (``until``) or
      been resumed. A row in the previous release's format (no ``until``) is carried only
      while the snapshot still shows that very hold (HomeKit copies the cloud's hold while
      the thermostat shows it), with the snapshot's end: under HomeKit an unknown end must not
      hand a running person's hold to the controller."""
    since = _homekit_since(snap)
    q = (
        select(ControlAction)
        .where(
            ControlAction.unit_key == unit_key,
            ControlAction.status == "skipped",
            ControlAction.request["kind"].astext == MANUAL_KIND,
        )
        .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
        .limit(20)
    )
    for det in session.execute(q).scalars():
        if latest is not None and (latest.ts, latest.id) >= (det.ts, det.id):
            return None  # a write since then supersedes every older row
        req = det.request if isinstance(det.request, dict) else {}
        if req.get("source") == HOMEKIT_SOURCE:
            if since is not None and det.ts < since:
                continue  # an earlier outage's hand change: the cloud has reported since
            return _person_from(det, req, until=None)
        if resume_logged_for(session, det) is not None:
            return None  # that hold was resumed
        if "until" in req:
            until = _parse_dt(req.get("until"))
        else:
            shown = snap.hold if snap is not None else None
            if shown is None or shown.set_by_us or not same_signature(req, hold_signature(shown)):
                return None  # an old-format row whose hold the thermostat no longer shows
            until = person_until(shown)
            req = {**req, "start": shown.start.isoformat() if shown.start is not None else req.get("start"),
                   "hold_type": shown.hold_type}
        if until is not None and until <= now:
            return None
        return _person_from(det, req, until=until)
    return None


def _person_from(det: ControlAction, req: dict[str, Any], until: datetime | None) -> PersonHold:
    return PersonHold(
        since=_parse_dt(req.get("start")) or det.ts, first_seen=det.ts,
        by="app" if req.get("by") == "app" else "thermostat",
        hold_type=req.get("hold_type") if isinstance(req.get("hold_type"), str) else None, until=until,
        heat_f=_num(req.get("heat_f")), cool_f=_num(req.get("cool_f")),
        climate_ref=req.get("climate_ref") if isinstance(req.get("climate_ref"), str) else None,
        detection_id=det.id,
    )


def _homekit_since(snap: UnitSnapshot | None) -> datetime | None:
    """When HomeKit took over this unit's live snapshot (the homekit service stamps it in the
    snapshot's settings, ISO UTC); None when not stamped."""
    raw = snap.settings.get("homekit_since") if snap is not None else None
    since = _parse_dt(raw)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return since


def vanished_person_hold(session: Session, unit_key: str, snap: UnitSnapshot, now: datetime) -> ControlAction | None:
    """The detection row of a person's hold that a cloud snapshot no longer shows and that did
    not end on its own: someone pressed Resume (at the thermostat or in the ecobee app). None
    when nothing vanished, when it vanished at or after its ``until`` (± 5 min: it ended on
    its own, bug 5), when a write since explains it, for a snapshot not newer than the row
    (it cannot show the hold gone), or for HomeKit's own rows (that kind of person hold just
    ends when the cloud returns)."""
    if snap.source == "homekit":
        return None
    if snap.hold is not None and snap.hold.hold_type not in AUTO_EVENTS:
        return None
    det = latest_detection(session, unit_key)
    if det is None or snap.ts <= det.ts:
        return None  # nothing logged, or a snapshot from before it was logged (e.g. a write the source
        # refused over a hold set since this snapshot): only a newer one can show it gone
    req = det.request if isinstance(det.request, dict) else {}
    if req.get("source") == HOMEKIT_SOURCE or "until" not in req:
        return None  # HomeKit's row, or an old-format row that cannot tell
    latest = latest_write(session, unit_key)
    if latest is not None and (latest.ts, latest.id) > (det.ts, det.id):
        return None
    until = _parse_dt(req.get("until"))
    if until is not None and snap.ts >= until - END_SLACK:
        return None
    return det


def pending_resume(
    session: Session, unit_key: str, snap: UnitSnapshot | None, control: ControlSettings, now: datetime,
) -> dict[str, Any] | None:
    """A Resume the thermostat shows that is not logged yet: the request of the RESUME_KIND
    row to log (the controller logs it this tick), else None. Either a person's hold vanished
    early (``vanished_person_hold``) or ours did (``cancelled_write``)."""
    if snap is None:
        return None
    det = vanished_person_hold(session, unit_key, snap, now)
    if det is not None:
        if resume_logged_for(session, det) is not None:
            return None
        return {"kind": RESUME_KIND, "person_hold_detection_id": det.id}
    write = cancelled_write(session, unit_key, snap, control, now)
    if write is None or resume_detection(session, unit_key, write.ts) is not None:
        return None
    end = write_end(write, control.hold_hours)
    return {"kind": RESUME_KIND, "cancelled_action_id": write.id, "hold_end": end.isoformat() if end else None}


def resume_backoff(
    session: Session, unit_key: str, control: ControlSettings, now: datetime, pending: bool = False,
) -> tuple[datetime, datetime] | None:
    """(until, seen_at) while the resume back-off runs, else None.

    It starts at a Resume, counted from when it was first seen, and lasts
    ``resume_backoff_hours`` in full (bugs 1, 2): the controller's RESUME_KIND row (or now, for
    one it logs this tick: ``pending``), or the owner's verified "Resume schedule" (kind
    ``resume_schedule``, or an older owner resume with no kind) from its completed_at. A later
    verified "Back to automatic" (kind ``automatic``) cancels it, and so does anything newer
    that put a hold on the unit (a write, or a person's hold seen since): when that hold ends
    on its own the controller takes over without waiting."""
    hours = control.resume_backoff_hours
    if hours <= 0:
        return None
    # Only a start after this can still be running; every row older than it is irrelevant.
    since = now - timedelta(hours=hours)
    starts: list[datetime] = [now] if pending else []
    row = session.execute(
        select(func.max(ControlAction.ts))
        .where(ControlAction.unit_key == unit_key, ControlAction.ts > since, ControlAction.status == "skipped",
               ControlAction.request["kind"].astext == RESUME_KIND)
    ).scalar_one_or_none()
    if row is not None:
        starts.append(row)
    kind = ControlAction.request["kind"].astext
    owner = _owner_resume(session, unit_key, since, (kind == OWNER_RESUME_SCHEDULE) | kind.is_(None))
    if owner is not None and owner > since:
        starts.append(owner)
    if not starts:
        return None
    seen = max(starts)
    automatic = _owner_resume(session, unit_key, since, kind == OWNER_AUTOMATIC)
    if automatic is not None and automatic >= seen:
        return None
    newer_hold = session.execute(
        select(func.max(ControlAction.ts)).where(
            ControlAction.unit_key == unit_key, ControlAction.ts > since, ControlAction.action == "set_hold",
            ControlAction.status.in_(WRITE_STATUSES),
        )
    ).scalar_one_or_none()
    det = latest_detection(session, unit_key)
    if (newer_hold is not None and newer_hold > seen) or (det is not None and det.ts > seen):
        return None
    until = seen + timedelta(hours=hours)
    return (until, seen) if until > now else None


def _owner_resume(session: Session, unit_key: str, since: datetime, kind_clause: Any) -> datetime | None:
    """When the owner's newest verified resume of this kind (queued after ``since`` - 1 h)
    completed."""
    done = func.coalesce(ControlAction.completed_at, ControlAction.ts)
    return session.execute(
        select(func.max(done)).where(
            ControlAction.unit_key == unit_key, ControlAction.ts > since - timedelta(hours=1),
            ControlAction.actor == "owner", ControlAction.action == "resume_program",
            ControlAction.status == "verified", kind_clause,
        )
    ).scalar_one_or_none()


def _hours(req: dict[str, Any], default_hours: float) -> float:
    hours = req.get("hours", default_hours)
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        return float(default_hours)
    return float(hours)


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _parse_dt(v: object) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)  # 3.11+ accepts a trailing Z
        except ValueError:
            return None
    return None


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

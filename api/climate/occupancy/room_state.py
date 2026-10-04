"""Per-room occupancy states (blueprint §3): occupied / asleep / empty / unknown / no_target.

Uncertain means occupied. Sleep windows count as occupied (state 'asleep'). Rooms without
a sensor are schedule-only: 'asleep' inside their sleep window, otherwise 'unknown' (no
temperature, not a comfort target). The Foyer has no comfort target: 'no_target'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.house import ROOM_BY_KEY, ROOMS, SENSOR_BY_KEY
from climate.store.app_settings import OccupancySettings, SleepWindow
from climate.store.orm import LiveSensor, OccupancyEvent, Reading5m, RoomStateRow, Sensor
from climate.timeutil import SLOT, in_window, parse_hhmm, to_local

# A sensor whose newest reading is older than this is stale: its silence proves nothing.
FRESH = timedelta(minutes=15)
# How far back load_signals looks for the newest "occupied" event/reading. Longer than the
# largest house_empty_after_min (480 min) so the house-empty rule always sees enough.
LOOKBACK = timedelta(hours=12)


@dataclass
class SensorSignal:
    sensor_key: str
    room_key: str
    ts: datetime | None  # newest reading time
    has_occupancy: bool
    occupied: bool | None
    motion: bool | None
    seconds_since_motion: int | None
    seconds_since_occupancy: int | None
    online: bool
    last_occupied_at: datetime | None  # newest occupancy_events/readings True for this sensor


@dataclass
class RoomStateResult:
    room_key: str
    state: str
    confidence: float
    reason: str
    since: datetime | None = None


# ---------------------------------------------------------------------------------------
# pure rules
# ---------------------------------------------------------------------------------------


def sensor_name(sensor_key: str) -> str:
    s = SENSOR_BY_KEY.get(sensor_key)
    return s.name if s else sensor_key


def is_fresh(sig: SensorSignal, now: datetime) -> bool:
    return sig.online and sig.ts is not None and now - sig.ts <= FRESH


def active_now(sig: SensorSignal, now: datetime) -> bool:
    """A fresh sensor reporting occupancy or motion right now."""
    return is_fresh(sig, now) and (sig.occupied is True or sig.motion is True)


def last_activity(sig: SensorSignal, now: datetime) -> datetime | None:
    """Newest moment this sensor is known to have seen someone (never in the future)."""
    candidates: list[datetime] = []
    if sig.last_occupied_at is not None:
        candidates.append(sig.last_occupied_at)
    if sig.ts is not None:
        if sig.seconds_since_motion is not None and sig.seconds_since_motion >= 0:
            candidates.append(sig.ts - timedelta(seconds=sig.seconds_since_motion))
        if sig.seconds_since_occupancy is not None and sig.seconds_since_occupancy >= 0:
            candidates.append(sig.ts - timedelta(seconds=sig.seconds_since_occupancy))
        if sig.occupied is True or sig.motion is True:
            candidates.append(sig.ts)
    if not candidates:
        return None
    return min(max(candidates), now)


def has_occupancy_info(sig: SensorSignal) -> bool:
    return (
        sig.occupied is not None
        or sig.motion is not None
        or (sig.seconds_since_motion is not None and sig.seconds_since_motion >= 0)
        or (sig.seconds_since_occupancy is not None and sig.seconds_since_occupancy >= 0)
    )


def uncertainty(sig: SensorSignal, now: datetime) -> str | None:
    """Why this occupancy sensor can't be trusted to say 'nobody here', or None."""
    name = sensor_name(sig.sensor_key)
    if not sig.online:
        return f"the {name} sensor is offline"
    if sig.ts is None:
        return f"no reading yet from the {name} sensor"
    if now - sig.ts > FRESH:
        return f"no reading from the {name} sensor for {_minutes(now - sig.ts)} min"
    if not has_occupancy_info(sig):
        return f"the {name} sensor reports no occupancy data"
    return None


def active_sleep_window(room_key: str, local_now: datetime, settings: OccupancySettings) -> SleepWindow | None:
    for w in settings.sleep_windows.get(room_key, []):
        if in_window(local_now, w.start, w.end, w.days):
            return w
    return None


def window_start(local_now: datetime, w: SleepWindow) -> datetime:
    """Local start of the window instance that contains ``local_now``."""
    start = parse_hhmm(w.start)
    begin = local_now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if begin > local_now:  # we are in the after-midnight part of yesterday's window
        begin -= timedelta(days=1)
    return begin


def compute_room_states(
    now: datetime, tz: str, signals: list[SensorSignal], settings: OccupancySettings,
    previous: dict[str, RoomStateResult] | None = None,
) -> dict[str, RoomStateResult]:
    """One result per room in climate.house.ROOMS. Rules: inside a sleep window -> asleep
    (conf 0.9). Sensor reported occupied now, or last occupied within empty_after_min ->
    occupied. All occupancy-capable sensors online and fresh (<15 min) and nothing for
    empty_after_min -> empty. Anything stale/offline/unknown -> occupied with reason
    'uncertain: ...' (conf 0.5). Thermostat with no occupancy (the Toy Room Essential)
    contributes temperature only; its room's SmartSensor supplies occupancy.
    ``since`` carries over from ``previous`` when the state is unchanged.

    When the state changes (or there is no previous), ``since`` is the natural start of the
    new state where it is known: the sleep window's start, the last motion for 'occupied',
    last motion + empty_after_min for 'empty'; otherwise ``now``."""
    previous = previous or {}
    local_now = to_local(now, tz)
    empty_after = timedelta(minutes=settings.empty_after_min)
    by_room: dict[str, list[SensorSignal]] = {}
    for sig in signals:
        by_room.setdefault(sig.room_key, []).append(sig)

    out: dict[str, RoomStateResult] = {}
    for room in ROOMS:
        natural: datetime | None = None
        if not room.has_comfort_target:
            res = RoomStateResult(room.key, "no_target", 1.0, "Walk-through space with no comfort target.")
        elif (w := active_sleep_window(room.key, local_now, settings)) is not None:
            res = RoomStateResult(room.key, "asleep", 0.9, f"Inside the sleep window {w.start}–{w.end}.")
            natural = window_start(local_now, w)
        elif not room.has_sensor:
            windows = settings.sleep_windows.get(room.key, [])
            hint = f" (sleep window {windows[0].start}–{windows[0].end})" if windows else ""
            res = RoomStateResult(
                room.key, "unknown", 0.0,
                f"No sensor; outside its sleep window{hint}, so occupancy is unknown.",
            )
        else:
            res, natural = _sensored_room(room.key, by_room.get(room.key, []), now, empty_after)

        prev = previous.get(room.key)
        if prev is not None and prev.state == res.state and prev.since is not None:
            res.since = prev.since
        else:
            res.since = min(natural, now) if natural is not None else now
        out[room.key] = res
    return out


def _sensored_room(
    room_key: str, sigs: list[SensorSignal], now: datetime, empty_after: timedelta,
) -> tuple[RoomStateResult, datetime | None]:
    occ = [s for s in sigs if s.has_occupancy]
    if not occ:
        return RoomStateResult(room_key, "occupied", 0.5, "uncertain: no occupancy sensor is reporting for this room."), None

    live = [s for s in occ if active_now(s, now)]
    if live:
        names = _join([sensor_name(s.sensor_key) for s in live])
        return RoomStateResult(room_key, "occupied", 0.95, f"Motion or presence right now ({names})."), now

    lasts = [t for t in (last_activity(s, now) for s in occ) if t is not None]
    last = max(lasts) if lasts else None
    if last is not None and now - last < empty_after:
        reason = (
            f"Last motion {_minutes(now - last)} min ago; the room counts as empty after "
            f"{_minutes(empty_after)} min without any."
        )
        return RoomStateResult(room_key, "occupied", 0.85, reason), last

    doubts = [d for d in (uncertainty(s, now) for s in occ) if d]
    if doubts:
        return RoomStateResult(room_key, "occupied", 0.5, f"uncertain: {'; '.join(doubts)}."), None

    if last is not None:
        return (
            RoomStateResult(room_key, "empty", 0.8, f"No motion for {_minutes(now - last)} min."),
            last + empty_after,
        )
    return RoomStateResult(room_key, "empty", 0.8, f"No motion in the last {_minutes(empty_after)} min."), None


def house_empty(
    now: datetime, tz: str, states: dict[str, RoomStateResult], signals: list[SensorSignal],
    settings: OccupancySettings,
) -> tuple[bool, str]:
    """Empty only when phones_away is True AND no motion/occupancy anywhere for
    house_empty_after_min AND no room is asleep (outside every sleep window). Never from
    phones alone; unknown phones -> not empty.

    Also never empty while any room is 'occupied' (including 'uncertain'), so a stale or
    offline sensor keeps the house occupied."""
    if settings.phones_away is None:
        return False, "Phone presence is unknown, so the house counts as occupied."
    if settings.phones_away is False:
        return False, "An adult's phone is home."

    local_now = to_local(now, tz)
    for room_key in settings.sleep_windows:
        w = active_sleep_window(room_key, local_now, settings)
        if w is not None:
            return False, f"Inside the {_room_name(room_key)} sleep window ({w.start}–{w.end})."
    asleep = [k for k, s in states.items() if s.state == "asleep"]
    if asleep:
        return False, f"{_join([_room_name(k) for k in asleep])} asleep."

    occupied = [s for s in states.values() if s.state == "occupied"]
    if occupied:
        uncertain = [s for s in occupied if s.reason.startswith("uncertain")]
        if len(uncertain) == len(occupied):
            detail = uncertain[0].reason.removeprefix("uncertain:").strip()
            return False, f"Can't confirm the house is empty ({_room_name(uncertain[0].room_key)}): {detail}"
        names = _join([_room_name(s.room_key) for s in occupied if s not in uncertain])
        return False, f"{names} occupied."

    occ = [s for s in signals if s.has_occupancy]
    if not occ:
        return False, "No occupancy sensors are reporting, so the house counts as occupied."
    window = timedelta(minutes=settings.house_empty_after_min)
    live = [s for s in occ if active_now(s, now)]
    if live:
        return False, f"Motion right now ({_join([sensor_name(s.sensor_key) for s in live])})."
    lasts = [(t, s) for s in occ if (t := last_activity(s, now)) is not None]
    last: datetime | None = None
    if lasts:
        last, who = max(lasts, key=lambda p: p[0])
        if now - last < window:
            return False, (
                f"Motion {_minutes(now - last)} min ago ({sensor_name(who.sensor_key)}); the house counts as "
                f"empty after {_minutes(window)} min without motion."
            )
    doubts = [d for d in (uncertainty(s, now) for s in occ) if d]
    if doubts:
        return False, f"Can't confirm the house is empty: {doubts[0]}."
    if last is not None:
        return True, f"Phones away and no motion anywhere for {_minutes(now - last)} min."
    return True, f"Phones away and no motion anywhere in the last {_minutes(window)} min."


# ---------------------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------------------


def load_signals(session: Session, now: datetime) -> list[SensorSignal]:
    """Build SensorSignal for every active sensor from live_sensors + occupancy_events.

    ``last_occupied_at`` is the newest of: an occupancy/motion event with value True, a
    readings_5m slot with occupied True (taken as the slot's end) and the live row itself
    when it reports occupancy, all within the last 12 hours and never after ``now``."""
    since = now - LOOKBACK
    rows = session.execute(
        select(Sensor, LiveSensor)
        .outerjoin(LiveSensor, LiveSensor.sensor_key == Sensor.key)
        .where(Sensor.is_active.is_(True))
        .order_by(Sensor.sort, Sensor.key)
    ).all()

    events = dict(
        session.execute(
            select(OccupancyEvent.sensor_key, func.max(OccupancyEvent.ts))
            .where(OccupancyEvent.value.is_(True), OccupancyEvent.ts >= since, OccupancyEvent.ts <= now)
            .group_by(OccupancyEvent.sensor_key)
        ).all()
    )
    slots = dict(
        session.execute(
            select(Reading5m.sensor_key, func.max(Reading5m.ts))
            .where(Reading5m.occupied.is_(True), Reading5m.ts >= since, Reading5m.ts <= now)
            .group_by(Reading5m.sensor_key)
        ).all()
    )

    out: list[SensorSignal] = []
    for sensor, live in rows:
        last: datetime | None = events.get(sensor.key)
        slot = slots.get(sensor.key)
        if slot is not None:
            slot_end = min(slot + SLOT, now)
            last = slot_end if last is None else max(last, slot_end)
        if live is not None and live.ts is not None and live.ts <= now and (live.occupied or live.motion):
            last = live.ts if last is None else max(last, live.ts)
        out.append(
            SensorSignal(
                sensor_key=sensor.key,
                room_key=sensor.room_key,
                ts=live.ts if live is not None else None,
                has_occupancy=bool(sensor.has_occupancy),
                occupied=live.occupied if live is not None else None,
                motion=live.motion if live is not None else None,
                seconds_since_motion=live.seconds_since_motion if live is not None else None,
                seconds_since_occupancy=live.seconds_since_occupancy if live is not None else None,
                online=bool(live.online) if live is not None else False,
                last_occupied_at=last,
            )
        )
    return out


def persist_room_states(session: Session, now: datetime, states: dict[str, RoomStateResult]) -> None:
    """Insert one room_states row per room at ``now`` (on conflict do nothing)."""
    rows = [
        dict(ts=now, room_key=s.room_key, state=s.state, confidence=float(s.confidence), reason=s.reason[:1000])
        for s in states.values()
        if s.room_key in ROOM_BY_KEY
    ]
    if rows:
        session.execute(insert(RoomStateRow).values(rows).on_conflict_do_nothing())


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _room_name(room_key: str) -> str:
    r = ROOM_BY_KEY.get(room_key)
    return r.name if r else room_key


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def _join(names: list[str]) -> str:
    names = list(dict.fromkeys(names))
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]

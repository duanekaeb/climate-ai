"""Occupancy rules (blueprint §3): sleep windows, uncertainty, unsensored rooms, house empty."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from climate.house import ROOMS, SENSORS
from climate.occupancy.room_state import (
    RoomStateResult,
    SensorSignal,
    compute_room_states,
    house_empty,
    load_signals,
    persist_room_states,
)
from climate.store.app_settings import OccupancySettings, SleepWindow

TZ = "America/Chicago"


def at(day: int, hh: int, mm: int = 0) -> datetime:
    """July 2026 local time -> UTC. July 15 2026 is a Wednesday."""
    return datetime(2026, 7, day, hh, mm, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def quiet_signals(now: datetime, last_motion_min: int = 60, **overrides: dict) -> list[SensorSignal]:
    """Every sensor fresh and reporting nobody, last motion ``last_motion_min`` ago."""
    out = []
    for s in SENSORS:
        sig = SensorSignal(
            sensor_key=s.key, room_key=s.room_key, ts=now - timedelta(minutes=1), has_occupancy=s.has_occupancy,
            occupied=False if s.has_occupancy else None, motion=False if s.has_occupancy else None,
            seconds_since_motion=None, seconds_since_occupancy=None, online=True,
            last_occupied_at=now - timedelta(minutes=last_motion_min) if s.has_occupancy else None,
        )
        for k, v in overrides.get(s.key, {}).items():
            setattr(sig, k, v)
        out.append(sig)
    return out


def test_every_room_gets_a_state():
    now = at(15, 14)
    states = compute_room_states(now, TZ, quiet_signals(now), OccupancySettings())
    assert set(states) == {r.key for r in ROOMS}


def test_sleep_window_across_midnight():
    settings = OccupancySettings()
    # Wednesday 23:00 and Thursday 02:00 are inside the Girls' Room 20:30-07:00 window
    for now in (at(15, 23), at(16, 2)):
        states = compute_room_states(now, TZ, quiet_signals(now), settings)
        assert states["girls_room"].state == "asleep"
        assert states["girls_room"].confidence == 0.9
    # 07:30 is outside
    now = at(16, 7, 30)
    assert compute_room_states(now, TZ, quiet_signals(now), settings)["girls_room"].state == "empty"


def test_sleep_window_days_are_start_days():
    # school nights only: windows starting Sunday..Thursday (6, 0, 1, 2, 3)
    settings = OccupancySettings(sleep_windows={"girls_room": [SleepWindow(start="20:30", end="07:00",
                                                                           days=[6, 0, 1, 2, 3])]})
    fri_2am = at(17, 2)  # window started Thursday -> asleep
    sat_2am = at(18, 2)  # window would have started Friday -> not a sleep night
    assert compute_room_states(fri_2am, TZ, quiet_signals(fri_2am), settings)["girls_room"].state == "asleep"
    assert compute_room_states(sat_2am, TZ, quiet_signals(sat_2am), settings)["girls_room"].state == "empty"


def test_asleep_since_is_the_window_start():
    now = at(16, 2)
    states = compute_room_states(now, TZ, quiet_signals(now), OccupancySettings())
    assert states["girls_room"].since == at(15, 20, 30)


def test_stale_or_offline_sensor_means_occupied():
    now = at(15, 14)
    sigs = quiet_signals(now, **{"main.kitchen": {"ts": now - timedelta(minutes=40)},
                                 "bed.office": {"online": False}})
    states = compute_room_states(now, TZ, sigs, OccupancySettings())
    assert states["kitchen"].state == "occupied"
    assert states["kitchen"].reason.startswith("uncertain")
    assert states["kitchen"].confidence == 0.5
    assert states["office"].state == "occupied" and "offline" in states["office"].reason
    assert states["living_room"].state == "empty"


def test_recent_motion_and_live_occupancy():
    now = at(15, 14)
    sigs = quiet_signals(now, **{"main.school_room": {"occupied": True},
                                 "main.living_room": {"last_occupied_at": now - timedelta(minutes=10)}})
    states = compute_room_states(now, TZ, sigs, OccupancySettings())
    assert states["school_room"].state == "occupied" and states["school_room"].confidence == 0.95
    assert states["living_room"].state == "occupied" and "10 min ago" in states["living_room"].reason
    assert states["kitchen"].state == "empty"


def test_seconds_since_motion_counts_from_the_reading():
    now = at(15, 14)
    # read 5 min ago, motion 20 min before that -> 25 min ago -> still occupied (empty after 30)
    sigs = quiet_signals(now, **{"main.kitchen": {"ts": now - timedelta(minutes=5), "seconds_since_motion": 1200,
                                                  "last_occupied_at": None}})
    states = compute_room_states(now, TZ, sigs, OccupancySettings())
    assert states["kitchen"].state == "occupied"


def test_essential_thermostat_has_no_occupancy():
    now = at(15, 14)
    # the Toy Room's Essential is stale and reports nothing; its SmartSensor is fresh and quiet
    sigs = quiet_signals(now, **{"up.toy_room_tstat": {"ts": now - timedelta(hours=2)}})
    assert compute_room_states(now, TZ, sigs, OccupancySettings())["toy_room"].state == "empty"
    # without the SmartSensor the room is uncertain -> occupied
    sigs = [s for s in quiet_signals(now) if s.sensor_key != "up.toy_room"]
    st = compute_room_states(now, TZ, sigs, OccupancySettings())["toy_room"]
    assert st.state == "occupied" and st.reason.startswith("uncertain")


def test_unsensored_rooms_and_foyer():
    day, night = at(15, 14), at(15, 21)
    s_day = compute_room_states(day, TZ, quiet_signals(day), OccupancySettings())
    s_night = compute_room_states(night, TZ, quiet_signals(night), OccupancySettings())
    for key in ("twins_room", "olive_room"):
        assert s_day[key].state == "unknown"
        assert s_night[key].state == "asleep"
    assert s_day["foyer"].state == "no_target"
    assert s_night["foyer"].state == "no_target"


def test_since_carries_over_when_unchanged():
    now = at(15, 14)
    earlier = now - timedelta(hours=1)
    prev = {"kitchen": RoomStateResult("kitchen", "empty", 0.8, "x", since=earlier),
            "school_room": RoomStateResult("school_room", "empty", 0.8, "x", since=earlier)}
    sigs = quiet_signals(now, **{"main.school_room": {"occupied": True}})
    states = compute_room_states(now, TZ, sigs, OccupancySettings(), previous=prev)
    assert states["kitchen"].since == earlier  # unchanged
    assert states["school_room"].since == now  # changed: occupied now


def test_empty_since_is_last_motion_plus_window():
    now = at(15, 14)
    states = compute_room_states(now, TZ, quiet_signals(now, last_motion_min=50), OccupancySettings())
    assert states["kitchen"].state == "empty"
    assert states["kitchen"].since == now - timedelta(minutes=50) + timedelta(minutes=30)


def _empty(now: datetime, settings: OccupancySettings, sigs: list[SensorSignal]) -> tuple[bool, str]:
    states = compute_room_states(now, TZ, sigs, settings)
    return house_empty(now, TZ, states, sigs, settings)


def test_house_empty_needs_phones_away():
    now = at(15, 14)
    sigs = quiet_signals(now, last_motion_min=90)
    assert _empty(now, OccupancySettings(phones_away=None), sigs)[0] is False  # unknown phones
    assert _empty(now, OccupancySettings(phones_away=False), sigs)[0] is False
    ok, reason = _empty(now, OccupancySettings(phones_away=True), sigs)
    assert ok is True and "90 min" in reason


def test_house_empty_needs_45_minutes_without_motion():
    now = at(15, 14)
    settings = OccupancySettings(phones_away=True)
    # 40 min: every room is already "empty" (30 min) but the house needs 45
    ok, reason = _empty(now, settings, quiet_signals(now, last_motion_min=40))
    assert ok is False and "45 min" in reason
    assert _empty(now, settings, quiet_signals(now, last_motion_min=46))[0] is True


def test_house_never_empty_inside_a_sleep_window():
    now = at(15, 21)
    ok, reason = _empty(now, OccupancySettings(phones_away=True), quiet_signals(now, last_motion_min=120))
    assert ok is False and "sleep window" in reason


def test_house_not_empty_when_a_sensor_is_uncertain():
    now = at(15, 14)
    sigs = quiet_signals(now, last_motion_min=120, **{"bed.office": {"online": False}})
    ok, reason = _empty(now, OccupancySettings(phones_away=True), sigs)
    assert ok is False and "offline" in reason


def test_load_signals_and_persist(db):
    from sqlalchemy import select, text

    from climate.store.orm import RoomStateRow

    now = datetime.now(UTC).replace(microsecond=0)
    db.execute(text(
        "INSERT INTO live_sensors (sensor_key, ts, source, temp_f, occupied, motion, online) "
        "VALUES ('main.kitchen', :ts, 'simulator', 75.0, false, false, true)"), {"ts": now - timedelta(minutes=2)})
    db.execute(text(
        "INSERT INTO occupancy_events (ts, sensor_key, kind, value, source) "
        "VALUES (:ts, 'main.kitchen', 'motion', true, 'homekit')"), {"ts": now - timedelta(minutes=12)})
    db.execute(text(
        "INSERT INTO readings_5m (ts, sensor_key, temp_f, occupied, source) "
        "VALUES (:ts, 'main.living_room', 75.0, true, 'simulator')"), {"ts": now - timedelta(minutes=30)})
    sigs = {s.sensor_key: s for s in load_signals(db, now)}
    assert len(sigs) == len(SENSORS)
    assert sigs["main.kitchen"].last_occupied_at == now - timedelta(minutes=12)
    assert sigs["main.living_room"].last_occupied_at == now - timedelta(minutes=25)  # slot end
    assert sigs["main.living_room"].ts is None and sigs["main.living_room"].online is False
    assert sigs["up.toy_room_tstat"].has_occupancy is False

    states = compute_room_states(now, TZ, list(sigs.values()), OccupancySettings())
    assert states["kitchen"].state == "occupied"  # motion 12 min ago
    assert states["living_room"].state == "occupied"  # no live reading -> uncertain
    persist_room_states(db, now, states)
    persist_room_states(db, now, states)  # idempotent
    rows = db.execute(select(RoomStateRow).where(RoomStateRow.ts == now)).scalars().all()
    assert len(rows) == len(ROOMS)

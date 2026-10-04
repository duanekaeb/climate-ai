"""state.load_house_state from database rows, plus the occupancy priors fit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from climate.house import SENSORS
from climate.occupancy.priors import fit_priors, occupancy_probability
from climate.sources.base import UnitSnapshot
from climate.state import load_house_state
from climate.store.app_settings import OccupancySettings, put_setting
from climate.store.orm import ControlAction, LiveSensor, LiveUnit, ModelFit, RoomStateRow, WeatherHour

TZ = "America/Chicago"
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)  # Wednesday morning


def sensor(db, key: str, *, age_min: float = 1, temp: float | None = 75.0, humidity: float | None = None,
           occupied: bool | None = False, online: bool = True, now: datetime = NOW) -> None:
    values = dict(sensor_key=key, ts=now - timedelta(minutes=age_min), source="simulator", temp_f=temp,
                  humidity=humidity, occupied=occupied, motion=occupied, online=online)
    db.execute(insert(LiveSensor).values(**values).on_conflict_do_update(index_elements=[LiveSensor.sensor_key],
                                                                         set_=values))


def all_sensors(db, now: datetime = NOW, occupied: tuple[str, ...] = ()) -> None:
    for s in SENSORS:
        sensor(db, s.key, occupied=(s.room_key in occupied) if s.has_occupancy else None,
               humidity=48.0 if s.has_humidity else None, now=now)


def unit(db, key: str, running: list[str], now: datetime = NOW, **kw) -> None:
    snap = UnitSnapshot(unit_key=key, ts=now - timedelta(minutes=2), source="simulator", hvac_mode="cool",
                        equipment_running=running, heat_sp_f=68, cool_sp_f=76, **kw)
    db.add(LiveUnit(unit_key=key, ts=snap.ts, source="simulator", snapshot=snap.model_dump(mode="json")))


def test_rooms_temperatures_stale_and_unsensored(db):
    all_sensors(db, occupied=("toy_room",))
    sensor(db, "up.toy_room_tstat", temp=77.0, humidity=50.0, occupied=None)
    sensor(db, "up.toy_room", temp=78.0, occupied=True)
    sensor(db, "main.school_room", age_min=40, temp=71.0)
    db.commit()
    st = load_house_state(db, NOW)
    assert st.rooms["toy_room"].temp_f == 77.5 and st.rooms["toy_room"].humidity == 50.0
    assert st.rooms["toy_room"].state == "occupied"
    school = st.rooms["school_room"]
    assert school.temp_f is None and school.stale is True and school.state == "occupied"
    assert school.reason.startswith("uncertain")
    for key in ("twins_room", "olive_room", "foyer"):
        assert st.rooms[key].temp_f is None and st.rooms[key].sensor_keys == []
    assert st.rooms["twins_room"].state == "unknown" and st.rooms["foyer"].state == "no_target"
    assert st.rooms["hallway"].sensor_keys == ["main.hallway_tstat"]
    assert set(st.rooms["toy_room"].sensor_keys) == {"up.toy_room_tstat", "up.toy_room"}


def test_offsets_priorities_and_units(db):
    all_sensors(db)
    unit(db, "main", ["compCool1", "fan"])
    unit(db, "up", ["fan"])
    unit(db, "bed", ["heatPump"])
    db.add(ModelFit(kind="room_offsets", params={"offsets": {"toy_room": {"day": 1.2, "night": 0.4},
                                                             "kitchen": 0.8, "twins_room": {"day": 3.0}}},
                    status="active"))
    db.add(ControlAction(ts=NOW - timedelta(minutes=12), unit_key="main", actor="controller", mode="act",
                         channel="simulator", action="set_hold", status="verified", reason="x",
                         request={"heat_f": 68.0, "cool_f": 76.0}))
    db.add(ControlAction(ts=NOW - timedelta(minutes=2), unit_key="main", actor="controller", mode="suggest",
                         channel="none", action="set_hold", status="suggested", reason="y"))
    db.commit()
    st = load_house_state(db, NOW)
    assert st.rooms["toy_room"].offset_f == 1.2
    assert st.rooms["kitchen"].offset_f == 0.8
    assert st.rooms["twins_room"].offset_f is None  # never for a room without a sensor
    assert st.rooms["school_room"].is_priority and st.rooms["toy_room"].is_priority and st.rooms["office"].is_priority
    assert not st.rooms["kitchen"].is_priority
    assert (st.units["main"].call, st.units["up"].call, st.units["bed"].call) == ("cool", "fan", "heat")
    assert st.units["main"].last_change_at == NOW - timedelta(minutes=12)
    assert st.units["up"].last_change_at is None
    assert 100 < st.units["main"].age_s < 140

    night = NOW + timedelta(hours=13)  # 23:00 local
    all_sensors(db, now=night)
    db.commit()
    st = load_house_state(db, night)
    assert st.rooms["toy_room"].offset_f == 0.4
    assert st.rooms["girls_room"].is_priority and st.rooms["hallway"].is_priority


def test_missing_data_never_raises(db):
    db.add(LiveUnit(unit_key="up", ts=NOW, source="simulator", snapshot={"garbage": True}))
    db.commit()
    st = load_house_state(db, NOW)
    assert st.units["up"].snapshot is None and st.units["up"].call == "unknown"
    assert st.units["main"].snapshot is None and st.units["main"].age_s is None
    assert st.outdoor_temp_f is None and st.forecast_high_f is None and st.forecast_sunny is None
    assert st.house_empty is False
    assert all(r.state in ("occupied", "unknown", "no_target") for r in st.rooms.values())
    assert st.policy_version_id is not None


def test_weather(db):
    unit(db, "main", [], outdoor_temp_f=88.0)
    db.commit()
    assert load_house_state(db, NOW).outdoor_temp_f == 88.0  # no weather rows: the thermostat's outdoor
    day_start = datetime(2026, 7, 15, 0, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)
    rows = []
    for h in range(24):
        ts = day_start + timedelta(hours=h)
        kind = "observed" if ts <= NOW else "forecast"
        rows.append(dict(ts=ts, source="open-meteo", kind=kind, temp_f=78.0 + (h if h <= 16 else 32 - h),
                         cloud_cover=15.0 if 12 <= h < 18 else 90.0))
    db.execute(insert(WeatherHour).values(rows))
    db.commit()
    st = load_house_state(db, NOW)
    assert st.outdoor_temp_f == 88.0  # observed at 10:00 local
    assert st.forecast_high_f == 94.0
    assert st.forecast_sunny is True


def test_experiment_arm_overlay_and_failure(db, monkeypatch):
    import climate.experiments.switchback as sb

    monkeypatch.setattr(sb, "active_arm", lambda s, now, tz: (None, {"linked_offset_f": 2.5, "unknown": 1}))
    assert load_house_state(db, NOW).policy.linked_offset_f == 2.5

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("experiments not ready")

    monkeypatch.setattr(sb, "active_arm", boom)
    st = load_house_state(db, NOW)
    assert st.policy.linked_offset_f == 1.0 and st.policy_version_id is not None


def test_since_comes_from_room_state_history(db):
    all_sensors(db)
    for minutes, state in ((40, "occupied"), (20, "empty"), (10, "empty"), (3, "empty")):
        db.add(RoomStateRow(ts=NOW - timedelta(minutes=minutes), room_key="kitchen", state=state, confidence=0.8,
                            reason="r"))
    db.commit()
    st = load_house_state(db, NOW)
    assert st.rooms["kitchen"].state == "empty"
    assert st.rooms["kitchen"].since == NOW - timedelta(minutes=20)


def test_house_empty_from_database(db):
    all_sensors(db)
    put_setting(db, "occupancy", OccupancySettings(phones_away=True))
    db.commit()
    st = load_house_state(db, NOW)
    assert st.house_empty is True and "Phones away" in st.house_empty_reason
    db.execute(text("INSERT INTO occupancy_events (ts, sensor_key, kind, value, source) "
                    "VALUES (:ts, 'bed.office', 'motion', true, 'homekit')"), {"ts": NOW - timedelta(minutes=35)})
    db.commit()
    st = load_house_state(db, NOW)
    assert st.house_empty is False and "35 min ago" in st.house_empty_reason


def occupancy_history(db, end: datetime, days: int) -> None:
    """Kitchen busy 7-22 except weekday afternoons 12-18; Toy Room busy 7-22 every day."""
    from climate.store.orm import Reading5m

    rows = []
    t = end - timedelta(days=days)
    while t < end:
        local = t.astimezone(ZoneInfo(TZ))
        awake = 7 <= local.hour < 22
        afternoon = 12 <= local.hour < 18 and local.weekday() < 5
        rows.append(dict(ts=t, sensor_key="main.kitchen", temp_f=75.0, occupied=awake and not afternoon,
                         source="ecobee_report"))
        rows.append(dict(ts=t, sensor_key="up.toy_room", temp_f=76.0, occupied=awake, source="ecobee_report"))
        t += timedelta(minutes=5)
    for i in range(0, len(rows), 2000):
        db.execute(insert(Reading5m).values(rows[i:i + 2000]))


def test_fit_priors(db):
    end = datetime(2026, 7, 16, 5, 0, tzinfo=UTC)  # midnight local
    occupancy_history(db, end, days=14)
    fit_id = fit_priors(db, end + timedelta(hours=3))
    assert fit_id is not None
    fit = db.get(ModelFit, fit_id)
    assert fit.kind == "occupancy_priors" and fit.status == "active"
    assert "twins_room" not in fit.params["rooms"] and "foyer" not in fit.params["rooms"]
    wed_9 = datetime(2026, 7, 15, 9, 30, tzinfo=ZoneInfo(TZ))
    wed_14 = datetime(2026, 7, 15, 14, 30, tzinfo=ZoneInfo(TZ))
    sat_14 = datetime(2026, 7, 11, 14, 30, tzinfo=ZoneInfo(TZ))
    school = [0, 1, 2, 3, 4]
    assert occupancy_probability(fit.params, "kitchen", wed_9, school) == 1.0
    assert occupancy_probability(fit.params, "kitchen", wed_14, school) == 0.0  # main floor empty afternoons
    assert occupancy_probability(fit.params, "kitchen", sat_14, school) == 1.0
    assert occupancy_probability(fit.params, "toy_room", wed_14, school) == 1.0
    assert occupancy_probability(fit.params, "twins_room", wed_14, school) is None
    second = fit_priors(db, end + timedelta(hours=3))
    assert db.get(ModelFit, fit_id).status == "retired" and second != fit_id
    active = db.execute(select(ModelFit).where(ModelFit.kind == "occupancy_priors", ModelFit.status == "active"))
    assert len(active.scalars().all()) == 1


def test_fit_priors_needs_data(db):
    assert fit_priors(db, NOW) is None

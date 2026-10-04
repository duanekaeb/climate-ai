"""SimulatedHouse: determinism, physics targets, thermostat behaviour, holds, sensors."""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime, timedelta

import pytest

from climate.house import ROOMS, SENSORS
from climate.sources.base import HoldRequest
from climate.sources.openmeteo import WeatherHourIn
from climate.sources.simulator import SimulatedHouse

UNSENSORED = {r.key for r in ROOMS if not r.has_sensor}
SENSOR_ROOM = {s.key: s.room_key for s in SENSORS}
# 2026-07-15 is a Wednesday; 05:00 UTC is local midnight in America/Chicago (CDT).
HOT_DAY = datetime(2026, 7, 15, 5, tzinfo=UTC)


def local_hour(ts: datetime) -> int:
    return (ts.hour - 5) % 24


def summer_weather(
    day0: datetime, peak: float, low: float, sw_peak: float = 900.0, dp: float = 68.0, days: int = 2
) -> list[WeatherHourIn]:
    """A clear diurnal cycle in local (CDT) time: low at 04:00, high at 16:00."""
    out = []
    for h in range(-12, 24 * days + 12):
        ts = day0 + timedelta(hours=h)
        lh = local_hour(ts)
        temp = (peak + low) / 2 + (peak - low) / 2 * math.cos(2 * math.pi * (lh - 16) / 24)
        sw = max(0.0, sw_peak * math.sin(math.pi * (lh - 6.5) / 13.5)) if 6.5 < lh < 20 else 0.0
        out.append(
            WeatherHourIn(
                ts=ts,
                kind="observed",
                temp_f=temp,
                rh=None,
                dewpoint_f=min(dp, temp - 1),
                cloud_cover=10.0,
                shortwave_wm2=sw,
                wind_mph=5.0,
                precip_in=0.0,
            )
        )
    return out


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


async def run_day(
    weather: list[WeatherHourIn],
    holds: dict[str, tuple[float, float]],
    day0: datetime = HOT_DAY,
    hold_hours: tuple[int, int] = (10, 20),
) -> list:
    """Run one local day minute by minute, renewing the given holds hourly in hold_hours."""
    clock = Clock(day0)
    house = SimulatedHouse(seed=7, clock=clock, weather_hours=weather)
    house.advance_to(day0)
    end = day0 + timedelta(days=1)
    while clock.t < end:
        clock.t += timedelta(minutes=1)
        if clock.t.minute == 0 and hold_hours[0] <= local_hour(clock.t) < hold_hours[1]:
            for unit, (heat, cool) in holds.items():
                res = await house.set_hold(
                    HoldRequest(unit_key=unit, heat_f=heat, cool_f=cool, hours=2, reason="t")
                )
                assert res.ok, res.error
        house.advance_to(clock.t)
    return await house.fetch_runtime(day0, end)


def duty(rows: list, unit: str, h0: int, h1: int, field: str = "comp_cool1") -> float:
    sel = [r for r in rows if r.unit_key == unit and h0 <= local_hour(r.ts) < h1]
    assert sel
    return sum(getattr(r, field) for r in sel) / (300.0 * len(sel))


# --- determinism -----------------------------------------------------------------------


def test_history_is_deterministic_for_a_seed():
    end = datetime(2026, 8, 3, 12, tzinfo=UTC)
    start = end - timedelta(days=2)
    a, snaps_a = SimulatedHouse(seed=7).generate_history(start, end)
    b, snaps_b = SimulatedHouse(seed=7).generate_history(start, end)
    c, _ = SimulatedHouse(seed=8).generate_history(start, end)
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]
    assert [s.model_dump() for s in snaps_a] == [s.model_dump() for s in snaps_b]
    assert [r.model_dump() for r in a] != [r.model_dump() for r in c]


def test_live_stepping_matches_history_and_continues_seamlessly():
    start = datetime(2026, 8, 1, 5, tzinfo=UTC)
    mid, end = start + timedelta(days=1), start + timedelta(days=1, hours=4)
    whole, _ = SimulatedHouse(seed=7).generate_history(start, end)

    clock = Clock(mid)
    house = SimulatedHouse(seed=7, clock=clock)
    first, _ = house.generate_history(start, mid)
    assert house.now == mid
    t = mid
    while t < end:  # the worker polls about once a minute
        t += timedelta(minutes=1)
        house.advance_to(t)
    clock.t = end
    import asyncio

    live = asyncio.run(house.fetch_runtime(mid, end))
    assert len(live) == 4 * 12 * 3
    assert [r.model_dump() for r in first + live] == [r.model_dump() for r in whole]


def test_saved_state_round_trips_exactly():
    start = datetime(2026, 8, 1, 5, tzinfo=UTC)
    mid, end = start + timedelta(hours=10, minutes=7), start + timedelta(hours=13)
    ref = SimulatedHouse(seed=7)
    ref.generate_history(start, end)

    house = SimulatedHouse(seed=7)
    house.generate_history(start, mid)
    state = json.loads(json.dumps(house._to_state()))
    restored = SimulatedHouse(seed=7)
    assert restored._restore(state)
    restored.advance_to(end)
    assert restored.now == ref.now
    assert restored._T == ref._T
    tail = lambda h: [r.model_dump() for r in h._slots if r.ts >= mid]
    assert tail(restored) == tail(ref)
    assert not SimulatedHouse(seed=8)._restore(state)  # a different seed starts fresh
    assert not SimulatedHouse(seed=7)._restore({"version": 1, "seed": 7, "m": 5})  # corrupt -> fresh
    broken = json.loads(json.dumps(state))
    broken["units"]["up"]["call"] = "on"
    fresh = SimulatedHouse(seed=7)
    assert not fresh._restore(broken) and fresh.now is None


def test_history_generation_is_fast_and_hourly_snapshots():
    end = datetime(2026, 9, 1, 12, 3, tzinfo=UTC)
    start = end - timedelta(days=60)
    t0 = time.perf_counter()
    rows, snaps = SimulatedHouse(seed=7).generate_history(start, end)
    assert time.perf_counter() - t0 < 15
    assert len(rows) == 60 * 288 * 3
    assert len(snaps) == (60 * 24 + 1) * 3
    assert all(s.ts.minute == 0 for s in snaps)
    first_slot = start.replace(
        minute=0
    )  # 12:03 floors to the 12:00 slot; the open 12:00 slot at `end` is not emitted
    assert {r.ts for r in rows} == {first_slot + timedelta(minutes=5 * i) for i in range(60 * 288)}


# --- physics targets -------------------------------------------------------------------


async def test_upstairs_runs_more_when_main_floor_floats_warm():
    hot = summer_weather(HOT_DAY, peak=95, low=75)
    floated = await run_day(hot, {"up": (68, 77), "main": (68, 80)})
    held = await run_day(hot, {"up": (68, 77), "main": (68, 76)})
    up_floated, up_held = duty(floated, "up", 13, 19), duty(held, "up", 13, 19)
    assert up_floated >= 0.85
    assert up_floated - up_held >= 0.12
    assert 0.45 <= duty(held, "main", 13, 19) <= 0.75  # main floor held at 76
    # the bed wing does not care what the main floor does
    assert abs(duty(floated, "bed", 13, 19) - duty(held, "bed", 13, 19)) < 0.06


async def test_mild_day_little_runtime_and_winter_heats():
    may = datetime(2026, 5, 6, 5, tzinfo=UTC)
    mild = await run_day(summer_weather(may, peak=70, low=52, dp=48, sw_peak=850), {}, day0=may)
    assert sum(r.comp_cool1 + r.aux_heat1 for r in mild) / 60 < 0.25 * 3 * 24 * 60
    jan = datetime(2026, 1, 14, 6, tzinfo=UTC)
    cold = [
        WeatherHourIn(
            ts=w.ts,
            kind="observed",
            temp_f=w.temp_f,
            rh=70.0,
            dewpoint_f=None,
            cloud_cover=w.cloud_cover,
            shortwave_wm2=w.shortwave_wm2,
            wind_mph=8.0,
            precip_in=0.0,
        )
        for w in summer_weather(jan, peak=35, low=20, sw_peak=450)
    ]
    winter = await run_day(cold, {}, day0=jan)
    assert sum(r.comp_cool1 for r in winter) == 0
    for unit in ("main", "up", "bed"):
        heat = sum(r.aux_heat1 for r in winter if r.unit_key == unit)
        assert heat > 2 * 3600  # furnace heat is reported as auxHeat
        assert sum(r.comp_heat1 for r in winter if r.unit_key == unit) == 0


def test_runtime_slots_are_well_formed():
    end = datetime(2026, 7, 20, 5, tzinfo=UTC)
    rows, _ = SimulatedHouse(seed=7).generate_history(end - timedelta(days=5), end)
    assert any(r.comp_cool2 > 0 for r in rows)  # hot afternoons use stage 2
    for r in rows:
        for f in ("comp_cool1", "comp_cool2", "comp_heat1", "comp_heat2", "aux_heat1", "aux_heat2", "fan"):
            assert 0 <= getattr(r, f) <= 300
        assert r.comp_cool2 <= r.comp_cool1
        assert r.aux_heat2 <= r.aux_heat1
        assert r.fan == r.comp_cool1 + r.aux_heat1
        assert r.ts.minute % 5 == 0 and r.ts.second == 0
        assert r.cool_sp_f - r.heat_sp_f >= 3.0
        assert set(r.sensor_temps) == {s.key for s in SENSORS if s.unit_key == r.unit_key}


def test_smart_away_floats_the_empty_main_floor():
    start = datetime(2026, 7, 13, 5, tzinfo=UTC)  # a Monday, local midnight
    rows, _ = SimulatedHouse(seed=7).generate_history(start, start + timedelta(days=7))
    away = [r for r in rows if r.unit_key == "main" and r.climate_ref == "away"]
    assert away and all(r.cool_sp_f == 80.0 for r in away)
    assert all(7 <= local_hour(r.ts) < 20 for r in away)  # empty by day only, never in the sleep program
    weekday_afternoons = {(r.ts - timedelta(hours=5)).date() for r in away if 15 <= local_hour(r.ts) < 17}
    assert len(weekday_afternoons) >= 4  # school ends ~15:00, dinner starts ~17:00
    asleep = [r for r in rows if r.unit_key == "main" and local_hour(r.ts) in (1, 2, 3)]
    assert all(r.climate_ref == "sleep" and r.cool_sp_f == 74.0 for r in asleep)


# --- sensors ---------------------------------------------------------------------------


async def test_unsensored_rooms_never_get_temperatures_and_essential_has_no_occupancy():
    end = datetime(2026, 7, 16, 5, tzinfo=UTC)
    house = SimulatedHouse(seed=7, clock=Clock(end))
    rows, snaps = house.generate_history(end - timedelta(days=1), end)
    snaps += await house.fetch_snapshots()
    keys = {s.key for s in SENSORS}
    for snap in snaps:
        assert {r.sensor_key for r in snap.sensors} <= keys
        assert not {SENSOR_ROOM[r.sensor_key] for r in snap.sensors} & UNSENSORED
        for reading in snap.sensors:
            assert reading.temp_f is not None
            if reading.sensor_key == "up.toy_room_tstat":
                assert reading.occupied is None and reading.motion is None
            if reading.sensor_key.endswith("_tstat"):
                assert reading.humidity is not None
            else:
                assert reading.humidity is None and reading.occupied is not None
        assert snap.settings == {"autoAway": True, "followMeComfort": False, "heatCoolMinDelta": 3.0}
    for r in rows:
        assert not {SENSOR_ROOM[k] for k in r.sensor_temps} & UNSENSORED
        if r.unit_key == "up":
            assert r.sensor_occupancy["up.toy_room_tstat"] is None
    up = next(s for s in snaps if s.unit_key == "up")
    assert up.model == "attisRetail"
    assert up.sensor_sets["sleep"] == ["up.girls_room"]
    main = next(s for s in snaps if s.unit_key == "main")
    assert main.sensor_sets["sleep"] == ["main.hallway_tstat"]
    assert set(main.sensor_sets["home"]) == {
        "main.hallway_tstat",
        "main.school_room",
        "main.living_room",
        "main.kitchen",
    }


async def test_occupancy_follows_the_schedule():
    start = datetime(2026, 7, 13, 5, tzinfo=UTC)  # Monday
    house = SimulatedHouse(seed=7)
    rows, _ = house.generate_history(start, start + timedelta(days=5))
    # seed 7 draws a school holiday on Monday 13 July (about 4% of school days are off)
    assert "school_room" not in house._day_plan(start.date())
    rows = [r for r in rows if r.ts >= start + timedelta(days=1)]

    def occ_share(sensor: str, h0: int, h1: int) -> float:
        unit = sensor.split(".")[0]
        sel = [r.sensor_occupancy[sensor] for r in rows if r.unit_key == unit and h0 <= local_hour(r.ts) < h1]
        return sum(bool(v) for v in sel) / len(sel)

    assert occ_share("main.school_room", 9, 11) > 0.9  # school days 8-15
    assert occ_share("main.school_room", 22, 24) == 0.0
    assert occ_share("bed.office", 9, 11) > 0.6  # office hours 8-17
    assert occ_share("up.toy_room", 16, 18) > 0.9  # weekdays 15-19
    assert occ_share("up.toy_room", 10, 12) == 0.0


# --- holds, revisions, catch-up -------------------------------------------------------


async def test_hold_readback_expiry_and_resume():
    t0 = datetime(2026, 7, 15, 17, 2, tzinfo=UTC)
    clock = Clock(t0)
    house = SimulatedHouse(seed=7, clock=clock)
    res = await house.set_hold(
        HoldRequest(unit_key="main", heat_f=68, cool_f=77.5, hours=1, reason="linked floors")
    )
    assert res.ok and res.channel == "simulator"
    assert res.readback["hold"]["cool_f"] == 77.5 and res.before["hold"] is None
    (snap,) = await house.fetch_snapshots(["main"])
    assert snap.cool_sp_f == 77.5 and snap.heat_sp_f == 68
    assert (
        snap.hold is not None
        and snap.hold.hold_type == "holdHours"
        and snap.hold.end == t0.replace(second=0) + timedelta(hours=1)
    )

    clock.t = t0 + timedelta(minutes=59)
    (snap,) = await house.fetch_snapshots(["main"])
    assert snap.hold is not None
    clock.t = t0 + timedelta(minutes=61)
    (snap,) = await house.fetch_snapshots(["main"])
    assert snap.hold is None and snap.cool_sp_f in (76.0, 80.0)  # back on the program (home or Smart Away)

    res = await house.set_hold(HoldRequest(unit_key="up", heat_f=67, cool_f=76, hours=2, reason="t"))
    assert res.ok
    res = await house.resume_program("up", "owner resumed")
    assert res.ok and res.before["hold"] is not None and res.readback["hold"] is None
    (snap,) = await house.fetch_snapshots(["up"])
    assert snap.hold is None


async def test_hold_rejections():
    house = SimulatedHouse(seed=7, clock=Clock(datetime(2026, 7, 15, 17, tzinfo=UTC)))
    bad_hours = HoldRequest.model_construct(unit_key="main", heat_f=68, cool_f=76, hours=3, reason="t")
    res = await house.set_hold(bad_hours)
    assert not res.ok and "holdHours" in res.error and res.readback["hold"] is None
    res = await house.set_hold(HoldRequest(unit_key="main", heat_f=74, cool_f=75, hours=1, reason="t"))
    assert not res.ok
    res = await house.set_hold(HoldRequest(unit_key="garage", heat_f=68, cool_f=76, hours=1, reason="t"))
    assert not res.ok and res.channel == "simulator"
    (snap,) = await house.fetch_snapshots(["main"])
    assert snap.hold is None


async def test_revisions_change_with_slot_and_setpoints():
    t0 = datetime(2026, 7, 15, 17, 1, tzinfo=UTC)
    clock = Clock(t0)
    house = SimulatedHouse(seed=7, clock=clock)
    r1 = await house.poll_revisions()
    assert set(r1) == {"main", "up", "bed"}
    clock.t = t0 + timedelta(minutes=2)
    assert await house.poll_revisions() == r1  # same slot, nothing changed
    await house.set_hold(HoldRequest(unit_key="bed", heat_f=66, cool_f=78, hours=1, reason="t"))
    r2 = await house.poll_revisions()
    assert r2["bed"] != r1["bed"] and r2["main"] == r1["main"]
    clock.t = t0 + timedelta(minutes=6)
    r3 = await house.poll_revisions()
    assert all(r3[k] != r2[k] for k in r3)  # a new 5-minute slot


async def test_fetch_runtime_returns_completed_slots_and_long_gaps_reset():
    t0 = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
    clock = Clock(t0)
    house = SimulatedHouse(seed=7, clock=clock)
    await house.fetch_snapshots()
    clock.t = t0 + timedelta(minutes=32)
    rows = await house.fetch_runtime(t0, clock.t)
    assert sorted({r.ts for r in rows}) == [t0 + timedelta(minutes=5 * i) for i in range(6)]
    assert len(rows) == 18
    later = t0 + timedelta(hours=9)
    clock.t = later
    snaps = await house.fetch_snapshots()
    assert house.now == later and all(s.ts == later for s in snaps)
    assert not await house.fetch_runtime(t0 + timedelta(hours=1), later)  # nothing invented for the gap
    health = await house.health()
    assert health.ok and health.kind == "simulator"


def test_synthetic_weather_is_seasonal_and_deterministic():
    from functools import partial

    from climate.sources.simulator import synthetic_weather
    from climate.store.app_settings import LocationSettings

    synth = partial(synthetic_weather, seed=7, location=LocationSettings())  # default mid-US climate, no DB
    jul = synth(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )
    jan = synth(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
    assert len(jul) == 31 * 24
    again = synth(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 7, 2, tzinfo=UTC),
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )
    assert again == jul[:24]
    mean = lambda rows: sum(r.temp_f for r in rows) / len(rows)
    assert 70 < mean(jul) < 85 and 25 < mean(jan) < 45
    assert max(r.temp_f for r in jul) > 90
    assert {r.kind for r in jul[: 19 * 24]} == {"observed"} and {r.kind for r in jul[19 * 24 :]} == {
        "forecast"
    }
    for r in jul:
        assert 0 <= r.cloud_cover <= 100 and 0 <= r.rh <= 100 and r.dewpoint_f <= r.temp_f
        lh = local_hour(r.ts)
        if lh < 5 or lh >= 21:
            assert r.shortwave_wm2 == 0
    assert max(r.shortwave_wm2 for r in jul) > 700


@pytest.mark.parametrize("seed", [1, 7])
def test_reset_starts_inside_the_comfort_band(seed):
    house = SimulatedHouse(seed=seed)
    house.advance_to(datetime(2026, 7, 15, 8, tzinfo=UTC))  # 03:00 local, the steady sleep program
    for snap in house._snapshots():
        assert snap.climate_ref == "sleep"
        assert snap.heat_sp_f - 1.5 <= snap.zone_temp_f <= snap.cool_sp_f + 1.5


async def test_get_source_builds_a_working_simulator(db):
    from climate.sources import get_source
    from climate.sources.base import ThermostatSource

    src = get_source("simulator")
    assert isinstance(src, ThermostatSource)
    snaps = await src.fetch_snapshots()
    assert len(snaps) == 3 and all(s.source == "simulator" and s.connected for s in snaps)
    assert set(await src.poll_revisions()) == {"main", "up", "bed"}
    await src.close()

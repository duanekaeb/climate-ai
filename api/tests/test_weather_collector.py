"""weather_hourly upserts, the Open-Meteo sync/backfill split, and the simulator's DB mode."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from climate.collector import weather
from climate.sources import openmeteo
from climate.sources.openmeteo import WeatherHourIn
from climate.store.app_settings import LocationSettings, get_raw, put_setting
from climate.store.orm import WeatherHour


def hour(ts: datetime, kind: str = "observed", temp: float = 70.0) -> WeatherHourIn:
    return WeatherHourIn(
        ts=ts,
        kind=kind,
        temp_f=temp,
        rh=50.0,
        dewpoint_f=50.0,
        cloud_cover=20.0,
        shortwave_wm2=100.0,
        wind_mph=4.0,
        precip_in=0.0,
    )


def set_location(db, lat=38.6, lon=-90.2):
    put_setting(db, "location", LocationSettings(lat=lat, lon=lon, tz="America/Chicago"))
    db.commit()


def test_upsert_is_idempotent_and_keeps_forecast_next_to_observed(db):
    t0 = datetime(2026, 10, 4, 12, tzinfo=UTC)
    rows = [hour(t0 + timedelta(hours=i), "forecast", 60.0 + i) for i in range(5)]
    assert weather.upsert_weather(db, rows, "open-meteo") == 5
    assert weather.upsert_weather(db, rows, "open-meteo") == 5
    db.commit()
    assert db.scalar(select(func.count()).select_from(WeatherHour)) == 5

    # the 12:00 hour is now observed (and warmer than forecast); a duplicate in one batch keeps the last
    observed = [
        hour(t0, "observed", 70.0),
        hour(t0, "observed", 71.0),
        {"ts": t0 + timedelta(hours=1), "kind": "observed", "temp_f": 62},
    ]
    assert weather.upsert_weather(db, observed, "open-meteo") == 2
    db.commit()
    kinds = db.execute(
        select(WeatherHour.kind, WeatherHour.temp_f).where(WeatherHour.ts == t0).order_by(WeatherHour.kind)
    ).all()
    assert kinds == [("forecast", 60.0), ("observed", 71.0)]
    assert db.scalar(select(func.count()).select_from(WeatherHour)) == 7

    # an update overwrites values (and skips junk rows)
    assert (
        weather.upsert_weather(db, [hour(t0, "forecast", 58.0), {"ts": None, "kind": "x"}], "open-meteo") == 1
    )
    db.commit()
    assert (
        db.scalar(select(WeatherHour.temp_f).where(WeatherHour.ts == t0, WeatherHour.kind == "forecast"))
        == 58.0
    )
    assert weather.upsert_weather(db, [], "open-meteo") == 0


async def test_sync_weather_without_location_is_a_no_op(db):
    assert await weather.sync_weather(datetime(2026, 10, 4, 12, tzinfo=UTC)) == 0


async def test_sync_weather_stores_open_meteo_hours(db, monkeypatch):
    set_location(db)
    now = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)
    calls = {}

    async def fake_forecast(lat, lon, at, past_days=2, forecast_days=3, **kw):
        calls.update(lat=lat, lon=lon, past=past_days, ahead=forecast_days)
        return [
            hour(now.replace(minute=0) + timedelta(hours=h), "observed" if h <= 0 else "forecast")
            for h in range(-48, 72)
        ]

    monkeypatch.setattr(openmeteo, "fetch_forecast", fake_forecast)
    assert await weather.sync_weather(now) == 120
    assert calls == {"lat": 38.6, "lon": -90.2, "past": 2, "ahead": 3}
    db.rollback()
    counts = dict(
        db.execute(
            select(WeatherHour.kind, func.count())
            .where(WeatherHour.source == "open-meteo")
            .group_by(WeatherHour.kind)
        ).all()
    )
    assert counts == {"observed": 49, "forecast": 71}


async def test_backfill_uses_archive_for_old_days_and_forecast_for_recent(db, monkeypatch):
    set_location(db)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 4, 6, tzinfo=UTC)
    seen = {}

    async def fake_archive(lat, lon, d0, d1, **kw):
        seen["archive"] = (d0, d1)
        out, t = (
            [],
            datetime(d0.year, d0.month, d0.day, tzinfo=UTC) - timedelta(hours=3),
        )  # spills before start
        while t.date() <= d1:
            out.append(hour(t))
            t += timedelta(hours=1)
        return out

    async def fake_forecast(lat, lon, at, past_days=2, forecast_days=3, **kw):
        seen["forecast"] = (past_days, forecast_days)
        t0 = datetime(2026, 10, 4, tzinfo=UTC) - timedelta(days=past_days - 1)
        return [
            hour(t0 + timedelta(hours=h), "observed" if t0 + timedelta(hours=h) < at else "forecast")
            for h in range(24 * (past_days + forecast_days))
        ]

    monkeypatch.setattr(openmeteo, "fetch_archive", fake_archive)
    monkeypatch.setattr(openmeteo, "fetch_forecast", fake_forecast)
    n = await weather.backfill_weather(start, end, now=now)
    assert seen["archive"] == (date(2026, 9, 1), date(2026, 9, 28))  # older than 5 days
    assert seen["forecast"] == (6, 1)  # 2026-09-29 .. today via past_days
    expected = int((end - start).total_seconds() // 3600)
    assert n == expected
    db.rollback()
    rows = (
        db.execute(select(WeatherHour.ts).where(WeatherHour.source == "open-meteo").order_by(WeatherHour.ts))
        .scalars()
        .all()
    )
    assert rows[0] == start and rows[-1] == end - timedelta(hours=1) and len(rows) == expected


async def test_backfill_without_location_returns_zero(db):
    assert (
        await weather.backfill_weather(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))
        == 0
    )


# --- the simulator's database mode -----------------------------------------------------


async def test_simulator_db_mode_uses_real_weather_persists_and_publishes(db):
    from climate.sources.simulator import STATE_KEY, SimulatedHouse

    t0 = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    weather.upsert_weather(
        db, [hour(t0 + timedelta(hours=h), "observed", 101.0) for h in range(-12, 4)], "open-meteo"
    )
    db.commit()

    class Clock:
        t = t0

        def __call__(self):
            return self.t

    clock = Clock()
    house = SimulatedHouse.from_settings(clock=clock)
    snaps = await house.fetch_snapshots()
    assert {s.outdoor_temp_f for s in snaps} == {101.0}  # real weather wins over synthetic
    await house.close()
    db.rollback()
    state = get_raw(db, STATE_KEY)
    assert state["m"] == int(t0.timestamp() // 60) and set(state["zones"]) == {"main", "up", "bed"}

    # synthetic weather is published only for hours without a real row
    sim_hours = set(db.execute(select(WeatherHour.ts).where(WeatherHour.source == "simulator")).scalars())
    assert sim_hours and t0 not in sim_hours and t0 + timedelta(hours=10) in sim_hours
    kinds = set(db.execute(select(WeatherHour.kind).where(WeatherHour.source == "simulator")).scalars())
    assert kinds == {"forecast"}  # the past hours around t0 are covered by real rows

    clock.t = t0 + timedelta(minutes=20)
    restored = SimulatedHouse.from_settings(clock=clock)
    assert restored.now == t0
    rows = await restored.fetch_runtime(t0, clock.t)
    assert len(rows) == 4 * 3


def test_simulator_history_and_synthetic_weather_for_backfill(db):
    from climate.sources.simulator import SimulatedHouse

    house = SimulatedHouse.from_settings()
    end = datetime(2026, 8, 1, 5, tzinfo=UTC)
    rows, snaps = house.generate_history(end - timedelta(days=1), end)
    assert len(rows) == 288 * 3 and len(snaps) == 25 * 3
    hours = house.synthetic_weather(end - timedelta(days=1), end)
    assert len(hours) == 24 and {h.kind for h in hours} == {"observed"}
    assert weather.upsert_weather(db, hours, "simulator") == 24
    db.commit()
    # the runtime record's outdoor temperature follows the stored synthetic weather
    by_hour = {h.ts: h.temp_f for h in hours}
    on_hour = [r for r in rows if r.ts.minute == 0 and r.unit_key == "main" and r.ts in by_hour]
    assert all(abs(r.outdoor_temp_f - by_hour[r.ts]) < 1.5 for r in on_hour)


def test_module_synthetic_weather_matches_the_configured_house(db):
    from climate.sources.simulator import SimulatedHouse, synthetic_weather

    set_location(db, lat=30.3, lon=-97.7)  # somewhere warmer than the default
    start, end = datetime(2026, 1, 10, tzinfo=UTC), datetime(2026, 1, 12, tzinfo=UTC)
    now = datetime(2026, 1, 11, tzinfo=UTC)
    house = SimulatedHouse.from_settings()
    configured = synthetic_weather(start, end, now=now)
    assert configured == house.synthetic_weather(start, end, now=now)
    default = synthetic_weather(start, end, now=now, location=LocationSettings())
    assert sum(h.temp_f for h in configured) > sum(h.temp_f for h in default)

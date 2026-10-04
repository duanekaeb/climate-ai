"""Analytics on hand-made data: degree-hours, fits, DST days, today's metrics, comfort,
equipment metrics, insufficient data and query performance."""

from __future__ import annotations

import math
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import text

TZ = "America/Chicago"


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _row(day: date, cool_s: float, temps: list[float], unit: str = "bed", heat_s: float = 0.0, slots: int = 288):
    from climate.analytics.daily import DayRow

    return DayRow(day=day, unit_key=unit, cool_s=cool_s, heat_s=heat_s, aux_s=0.0, fan_s=cool_s + heat_s,
                  slots=slots, mode="cool" if cool_s >= heat_s else "heat", outdoor_mean_f=float(np.mean(temps)),
                  outdoor_max_f=float(np.max(temps)), hourly_outdoor_f=list(temps), expected_slots=288)


def _diurnal(mean: float, swing: float = 10.0) -> list[float]:
    return [mean + swing * math.sin((h - 10) / 24 * 2 * math.pi) for h in range(24)]


def _insert_runtime(db, rows: list[dict]) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from climate.store.orm import Runtime5m

    for i in range(0, len(rows), 2000):
        db.execute(insert(Runtime5m).values(rows[i : i + 2000]).on_conflict_do_nothing())
    db.flush()


def _rt(ts: datetime, unit: str, cool: int = 0, heat1: int = 0, aux1: int = 0, mode: str = "cool", out: float = 80.0):
    return dict(ts=ts, unit_key=unit, comp_cool1=cool, comp_cool2=0, comp_heat1=heat1, comp_heat2=0, aux_heat1=aux1,
                aux_heat2=0, fan=cool + heat1 + aux1, hvac_mode=mode, zone_temp_f=75.0, outdoor_temp_f=out,
                source="ecobee_report")


def _slots(t0: datetime, t1: datetime) -> list[datetime]:
    out, t = [], t0
    while t < t1:
        out.append(t)
        t += timedelta(minutes=5)
    return out


# ---------------------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------------------


def test_degree_hours():
    from climate.analytics.daily import degree_hours

    temps = [60.0] * 12 + [80.0] * 12
    assert degree_hours(temps, 65, "cool") == pytest.approx(12 * 15 / 24)
    assert degree_hours(temps, 65, "heat") == pytest.approx(12 * 5 / 24)
    assert degree_hours([], 65, "cool") == 0.0
    assert degree_hours([70.0] * 25, 65, "cool") == pytest.approx(25 * 5 / 24)  # a 25-hour DST day
    with pytest.raises(ValueError):
        degree_hours(temps, 65, "fan")


def test_fit_recovers_known_heating_baseline():
    """Varied winter weather, true bp 60°F, 2000 s/HDD, intercept 0, small noise."""
    from climate.analytics.baseline import fit_baseline

    rng = np.random.default_rng(3)
    rows = []
    for i in range(60):
        temps = _diurnal(25 + 40 * (i % 13) / 12, swing=8)
        hdd = sum(max(0.0, 60 - t) for t in temps) / 24
        rows.append(_row(date(2026, 1, 1) + timedelta(days=i), 0.0, temps, heat_s=max(0.0, 2000 * hdd + rng.normal(0, 400))))
    fit = fit_baseline(rows, "heat")
    assert fit is not None
    assert abs(fit.balance_point_f - 60) <= 3
    assert fit.slope_s_per_dd == pytest.approx(2000, rel=0.15)
    assert fit.intercept_s >= 0
    assert fit.passes, fit.check_summary()
    assert fit.n_days == 60 and fit.train_start == date(2026, 1, 1)
    assert fit_baseline(rows, "cool") is None  # no cooling runtime -> nothing to model


def test_insufficient_data_returns_none():
    from climate.analytics.baseline import fit_baseline

    rows = [_row(date(2026, 7, 1) + timedelta(days=i), 20000 + 500 * i, _diurnal(80 + i % 5)) for i in range(20)]
    assert fit_baseline(rows, "cool") is None  # 20 < 21 complete days
    rows.append(_row(date(2026, 7, 21), 21000, _diurnal(81)))
    partial = [_row(d.day, d.cool_s, d.hourly_outdoor_f, slots=200) for d in rows]
    assert fit_baseline(partial, "cool") is None  # partial days never count
    assert fit_baseline(rows, "cool") is not None
    with pytest.raises(ValueError):
        fit_baseline(rows + [_row(date(2026, 8, 1), 1.0, [80.0] * 24, unit="up")], "cool")


def test_residual_interval_widens_with_autocorrelation():
    from climate.analytics.baseline import ResidualStats

    white = ResidualStats(sigma_s=1000, rho=0.0, n=90, p=2, mean_y_s=20000)
    sticky = ResidualStats(sigma_s=1000, rho=0.6, n=90, p=2, mean_y_s=20000)
    assert sticky.halfwidth(14) > white.halfwidth(14) > 0
    assert white.halfwidth(28) > white.halfwidth(14)  # absolute width of a longer sum grows
    assert white.halfwidth(14, mean_expected_day_s=40000) == pytest.approx(2 * white.halfwidth(14))


# ---------------------------------------------------------------------------------------
# DST days
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("day,slots,hours", [(date(2026, 11, 1), 300, 25), (date(2026, 3, 8), 276, 23)])
def test_dst_day_has_its_real_length(db, day, slots, hours):
    from climate.analytics.daily import daily_rows, is_complete
    from climate.store.orm import WeatherHour
    from climate.timeutil import day_bounds_utc

    t0, t1 = day_bounds_utc(day, TZ)
    _insert_runtime(db, [_rt(t, "main", cool=100) for t in _slots(t0, t1)])
    t = t0
    while t < t1:
        db.add(WeatherHour(ts=t, source="open-meteo", kind="observed", temp_f=70.0))
        t += timedelta(hours=1)
    db.flush()
    rows = daily_rows(db, day, day, TZ)
    assert len(rows) == 1
    r = rows[0]
    assert (r.slots, r.expected_slots, len(r.hourly_outdoor_f)) == (slots, slots, hours)
    assert r.cool_s == 100 * slots
    assert r.cdd65 == pytest.approx(hours * 5 / 24, abs=1e-3)
    assert is_complete(r)
    assert r.weather_sources == ["open-meteo"]


def test_outdoor_falls_back_and_interpolates(db):
    """No weather rows at all: the thermostat's outdoor reading is used; a 2-hour hole in the
    weather table is filled linearly."""
    from climate.analytics.daily import daily_rows
    from climate.store.orm import WeatherHour
    from climate.timeutil import day_bounds_utc

    d = date(2026, 7, 10)
    t0, t1 = day_bounds_utc(d, TZ)
    _insert_runtime(db, [_rt(t, "up", cool=50, out=90.0) for t in _slots(t0, t1)])
    r = daily_rows(db, d, d, TZ)[0]
    assert len(r.hourly_outdoor_f) == 24 and set(r.hourly_outdoor_f) == {90.0}
    assert r.weather_sources == ["runtime_5m"]

    d2 = date(2026, 7, 11)
    a, b = day_bounds_utc(d2, TZ)
    _insert_runtime(db, [_rt(t, "up", cool=50, out=None) for t in _slots(a, b)])  # type: ignore[arg-type]
    for h in range(24):
        if h in (10, 11):
            continue
        db.add(WeatherHour(ts=a + timedelta(hours=h), source="open-meteo", kind="observed", temp_f=70.0 + h))
    db.flush()
    r2 = daily_rows(db, d2, d2, TZ)[0]
    assert len(r2.hourly_outdoor_f) == 24
    assert r2.hourly_outdoor_f[10] == pytest.approx(80.0) and r2.hourly_outdoor_f[11] == pytest.approx(81.0)
    assert set(r2.weather_sources) == {"open-meteo", "interpolated"}


# ---------------------------------------------------------------------------------------
# equipment: which column is the heating metric
# ---------------------------------------------------------------------------------------


def test_heating_metric_follows_equipment(db):
    from climate.analytics.daily import daily_rows
    from climate.timeutil import day_bounds_utc

    d = date(2026, 1, 15)
    t0, t1 = day_bounds_utc(d, TZ)
    rows = []
    for t in _slots(t0, t1):
        rows.append(_rt(t, "main", heat1=100, aux1=20, mode="heat", out=30.0))  # heat pump + strips
        rows.append(_rt(t, "up", heat1=0, aux1=150, mode="heat", out=30.0))  # furnace
        rows.append(_rt(t, "bed", heat1=60, aux1=10, mode="heat", out=30.0))  # unknown, reports comp heat
    _insert_runtime(db, rows)
    db.execute(text("UPDATE units SET equipment = '{\"heating\": \"heat_pump\"}' WHERE key = 'main'"))
    db.execute(text("UPDATE units SET equipment = '{\"heating\": \"furnace\"}' WHERE key = 'up'"))
    out = {r.unit_key: r for r in daily_rows(db, d, d, TZ)}
    assert (out["main"].heat_s, out["main"].aux_s) == (100 * 288, 20 * 288)
    assert (out["up"].heat_s, out["up"].aux_s) == (150 * 288, 0.0)  # furnace: aux IS the heat, never doubled
    assert (out["bed"].heat_s, out["bed"].aux_s) == (60 * 288, 10 * 288)
    assert all(r.mode == "heat" for r in out.values())


# ---------------------------------------------------------------------------------------
# today's runtime, duty, maxed minutes (partial day)
# ---------------------------------------------------------------------------------------


def test_unit_today_partial_day(db):
    from climate.analytics.metrics import unit_today

    z = ZoneInfo(TZ)
    now = datetime(2026, 7, 15, 15, 20, tzinfo=z).astimezone(UTC)  # 15:20 local
    midnight = datetime(2026, 7, 15, 0, 0, tzinfo=z).astimezone(UTC)
    rows = []
    for t in _slots(midnight - timedelta(hours=2), now):  # yesterday's tail must not count
        local_h = t.astimezone(z).hour
        cool = 300 if local_h == 13 else 150  # 13:00-14:00 maxed out
        if t >= now - timedelta(hours=1):
            cool = 240
        rows.append(_rt(t, "up", cool=cool))
    _insert_runtime(db, rows)
    out = unit_today(db, now, TZ)
    assert set(out) == {"main", "up", "bed"}
    up = out["up"]
    today = [r for r in rows if r["ts"] >= midnight]
    assert up["today_runtime_min"] == pytest.approx(sum(r["comp_cool1"] for r in today) / 60, abs=0.1)
    assert up["duty_last_hour_pct"] == pytest.approx(80.0, abs=0.1)
    assert up["maxed_minutes_today"] == 60.0
    assert out["main"] == {"today_runtime_min": 0.0, "duty_last_hour_pct": None, "maxed_minutes_today": 0.0}


# ---------------------------------------------------------------------------------------
# comfort
# ---------------------------------------------------------------------------------------


def test_comfort_scores_occupied_minutes_against_the_band(db):
    from climate.analytics.metrics import comfort, comfort_period
    from climate.store.orm import Reading5m, RoomStateRow

    z = ZoneInfo(TZ)
    t0 = datetime(2026, 7, 15, 9, 0, tzinfo=z).astimezone(UTC)  # day band (up: 68-77)
    t1 = t0 + timedelta(hours=2)
    for i, t in enumerate(_slots(t0, t1)):
        # first hour inside the band (+0.4°F tolerance), second hour 1.5°F too warm
        temp = 77.4 if i < 12 else 78.5
        db.add(Reading5m(ts=t, sensor_key="up.girls_room", temp_f=temp, occupied=True, source="ecobee_report"))
        # office: no room_states -> falls back to its sensor; empty the whole time
        db.add(Reading5m(ts=t, sensor_key="bed.office", temp_f=90.0, occupied=False, source="ecobee_report"))
    # room_states every 3 minutes for the Girls' Room: occupied the first 90 min, then empty
    t = t0
    while t < t1:
        st = "occupied" if t < t0 + timedelta(minutes=90) else "empty"
        db.add(RoomStateRow(ts=t, room_key="girls_room", state=st, confidence=0.9, reason="test"))
        t += timedelta(minutes=3)
    db.flush()
    rows = {r["room_key"]: r for r in comfort_period(db, t0, t1, TZ)}
    girls = rows["girls_room"]
    assert girls["occupied_min"] == 90.0
    assert girls["in_band_pct"] == pytest.approx(100 * 12 / 18, abs=0.1)
    assert girls["worst_excursion_f"] == pytest.approx(1.5, abs=0.01)
    assert rows["office"]["occupied_min"] == 0.0 and rows["office"]["in_band_pct"] is None
    assert "twins_room" not in rows and "foyer" not in rows  # never invent a temperature
    assert rows["kitchen"]["occupied_min"] == 0.0  # no data at all -> nothing scored
    assert {r.room_key for r in comfort(db, days=7)} == set(rows)


# ---------------------------------------------------------------------------------------
# honesty when the data is short
# ---------------------------------------------------------------------------------------


def test_no_savings_claim_without_a_baseline(db):
    from climate.analytics.attribution import savings, waterfall
    from climate.store.orm import WeatherHour
    from climate.timeutil import day_bounds_utc

    start = date(2026, 7, 1)
    rows = []
    for i in range(12):
        d = start + timedelta(days=i)
        a, b = day_bounds_utc(d, TZ)
        for t in _slots(a, b):
            for u in ("main", "up", "bed"):
                rows.append(_rt(t, u, cool=120))
        for h in range(24):
            db.add(WeatherHour(ts=a + timedelta(hours=h), source="open-meteo", kind="observed", temp_f=85.0))
    _insert_runtime(db, rows)
    s = savings(db, date(2026, 7, 8), date(2026, 7, 12))
    assert s.baseline_ok is False
    assert s.savings_pct is None and s.ci90_low_pct is None and s.expected_min is None
    assert s.actual_min == pytest.approx(5 * 3 * 288 * 120 / 60)
    assert "baseline" in s.note and "No savings claim" in s.note
    assert all(d.expected_min is None for d in s.days)
    w = waterfall(db, date(2026, 7, 6))
    assert [i.label for i in w.items] == ["Last week", "This week"]
    assert w.strategy_ci90_min is None and "can't be separated" in w.note


# ---------------------------------------------------------------------------------------
# performance: aggregate in SQL, shape in numpy
# ---------------------------------------------------------------------------------------


def test_query_performance(db):
    from climate.analytics.daily import daily_rows
    from climate.timeutil import day_bounds_utc

    end = date(2026, 9, 30)
    start = end - timedelta(days=364)
    t0, t1 = day_bounds_utc(start, TZ)[0], day_bounds_utc(end, TZ)[1]
    db.execute(
        text(
            """
            INSERT INTO runtime_5m (ts, unit_key, comp_cool1, comp_cool2, comp_heat1, comp_heat2, aux_heat1,
                                    aux_heat2, fan, hvac_mode, zone_temp_f, outdoor_temp_f, source)
            SELECT t, u, (150 + 100 * sin(extract(epoch FROM t) / 86400.0))::int, 0, 0, 0, 0, 0, 0, 'cool', 76,
                   80, 'test'
            FROM generate_series(CAST(:t0 AS timestamptz), CAST(:t1 AS timestamptz) - interval '5 minutes',
                                 interval '5 minutes') AS t
            CROSS JOIN unnest(ARRAY['main', 'up', 'bed']) AS u
            """
        ),
        {"t0": t0, "t1": t1},
    )
    db.execute(
        text(
            """
            INSERT INTO weather_hourly (ts, source, kind, temp_f, shortwave_wm2)
            SELECT t, 'open-meteo', 'observed', 80 + 10 * sin(extract(epoch FROM t) / 13751.0), 300
            FROM generate_series(CAST(:t0 AS timestamptz), CAST(:t1 AS timestamptz) - interval '1 hour',
                                 interval '1 hour') AS t
            """
        ),
        {"t0": t0, "t1": t1},
    )
    db.commit()
    db.execute(text("ANALYZE runtime_5m"))
    db.execute(text("ANALYZE weather_hourly"))
    db.execute(text("SELECT count(*) FROM runtime_5m")).scalar()  # first read after a bulk load sets hint bits

    def best_of(n: int, fn):  # other test runs may share this machine: take the best of n
        out, best = None, math.inf
        for _ in range(n):
            t = time.perf_counter()
            out = fn()
            best = min(best, time.perf_counter() - t)
        return out, best

    rows90, t90 = best_of(3, lambda: daily_rows(db, end - timedelta(days=89), end, TZ))
    rows365, t365 = best_of(3, lambda: daily_rows(db, start, end, TZ))
    assert len(rows90) == 90 * 3 and len(rows365) == 365 * 3
    assert all(r.slots == r.expected_slots for r in rows365)
    assert sum(r.cool_s for r in rows90) > 0
    assert t90 < 1.0, f"90 days took {t90:.2f}s"
    assert t365 < 1.0, f"a year took {t365:.2f}s"
    print(f"\ndaily_rows: 90 days {t90 * 1000:.0f} ms, 365 days {t365 * 1000:.0f} ms")

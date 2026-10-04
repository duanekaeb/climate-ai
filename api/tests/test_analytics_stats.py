"""Statistical honesty checks: drift false alarms in a shoulder season, the single-day
prediction interval's coverage, the balance-point rule vs NMBE, the in-progress day's
expectation and the heating-metric cache. Monte Carlo tests use a fixed RNG."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import text

from climate.analytics import baseline, daily, metrics, reports
from climate.analytics.daily import DayRow

TZ = "America/Chicago"
SLOPE = 1500.0  # seconds per cooling degree-day


def _temps(mean: float, swing: float = 9.0) -> list[float]:
    return [mean + swing * math.sin((h - 10) / 24 * 2 * math.pi) for h in range(24)]


def _true_runtime(mean: float, bp: float = 65.0) -> float:
    return SLOPE * sum(max(0.0, t - bp) for t in _temps(mean)) / 24


def _row(d: date, mean: float, y: float, slots: int = 288, unit: str = "up") -> DayRow:
    temps = _temps(mean)
    return DayRow(day=d, unit_key=unit, cool_s=y * slots / 288, heat_s=0.0, aux_s=0.0, fan_s=y, slots=slots,
                  mode="cool" if y > 0 else None, outdoor_mean_f=mean, outdoor_max_f=max(temps),
                  hourly_outdoor_f=temps, expected_slots=288)


def _shoulder_days(rng: np.random.Generator, n: int, start: date, mean_of, mult: float = 0.12,
                   add: float = 250.0) -> list[DayRow]:
    """Cooling runtime = 1500 s/CDD(65) with noise that grows with runtime (12% of it plus a
    250 s floor), never below 0."""
    out = []
    for i in range(n):
        m = mean_of(i)
        y = max(0.0, _true_runtime(m) * (1 + mult * rng.standard_normal()) + add * rng.standard_normal())
        out.append(_row(start + timedelta(days=i), m, y))
    return out


def _shoulder(rng: np.random.Generator, seed_rng: np.random.Generator | None = None):
    """90 training days warming from ~62 to ~78°F (late spring), then a 7-day window at ~84°F:
    hotter than any training day, noisier in seconds, and NOT drifting."""
    d0 = date(2026, 3, 1)
    train = _shoulder_days(rng, 90, d0, lambda i: 62 + 16 * i / 89 + 3 * rng.standard_normal())
    recent = _shoulder_days(rng, 7, d0 + timedelta(days=90), lambda i: 84 + 2 * rng.standard_normal())
    return train, recent


# ---------------------------------------------------------------------------------------
# 1. drift: false alarms in a shoulder season
# ---------------------------------------------------------------------------------------


def test_drift_false_alarm_rate_in_a_shoulder_season():
    """Null scenario (the same physics before and during the window): the old z (training
    residual std, no level scaling) flagged about half of these weeks as drifting."""
    rng = np.random.default_rng(20261004)
    checked = alarms = 0
    n_sims = 500
    for _ in range(n_sims):
        train, recent = _shoulder(rng)
        fit = baseline.fit_baseline(train, "cool")
        assert fit is not None
        unit, why = metrics.drift_check(fit, recent)
        if unit is None:
            continue
        checked += 1
        alarms += unit.drifting
    assert checked >= 0.9 * n_sims  # the check really ran, it did not just skip
    assert alarms / checked <= 0.05, (alarms, checked)


def test_drift_check_skips_fits_and_modes_that_cannot_say_anything():
    rng = np.random.default_rng(5)
    train, recent = _shoulder(rng)
    fit = baseline.fit_baseline(train, "cool")
    assert fit is not None and fit.passes and fit.n_mode_days and fit.n_mode_days >= 10
    unit, why = metrics.drift_check(fit, recent)
    assert unit is not None and why is None and unit.recent_days == 7

    failing = baseline.BaselineFit(**{**fit.__dict__, "cvrmse": 0.31})
    unit, why = metrics.drift_check(failing, recent)
    assert unit is None and "fails its checks" in why and "CV(RMSE) 31.0%" in why

    few_train = baseline.BaselineFit(**{**fit.__dict__, "n_mode_days": 8})
    unit, why = metrics.drift_check(few_train, recent)
    assert unit is None and "only 8 training days" in why

    idle = [_row(r.day, r.outdoor_mean_f or 70.0, 0.0 if i < 3 else r.cool_s) for i, r in enumerate(recent)]
    unit, why = metrics.drift_check(fit, idle)
    assert unit is None and "ran on only 4 of 7" in why

    # A real 25% cut on the same week still shows.
    cut = [_row(r.day, r.outdoor_mean_f or 70.0, 0.75 * r.cool_s) for r in recent]
    unit, _ = metrics.drift_check(fit, cut)
    assert unit is not None and unit.drifting and unit.z < 0


def test_drift_note_says_what_was_skipped_and_why(db, monkeypatch):
    rng = np.random.default_rng(6)
    train, recent = _shoulder(rng)
    good = baseline.fit_baseline(train, "cool")
    bad = baseline.BaselineFit(**{**good.__dict__, "unit_key": "bed", "cvrmse": 0.4})
    end = metrics.local_date(metrics.utcnow(), TZ) - timedelta(days=1)
    rows = [_row(end - timedelta(days=6 - i), r.outdoor_mean_f, r.cool_s, unit=u)
            for u in ("up", "bed") for i, r in enumerate(recent)]
    fits = {("up", "cool"): good, ("bed", "cool"): bad}
    monkeypatch.setattr(metrics, "active_fits", lambda s: fits)
    monkeypatch.setattr(metrics, "pre_period_fits", lambda s, start, tz: fits)
    monkeypatch.setattr(metrics, "daily_rows", lambda s, a, b, tz: rows)
    report = metrics.drift(db)
    assert [(u.unit_key, u.mode) for u in report.units] == [("up", "cool")]
    assert "Not checked: bed/office wing cooling: its baseline fails its checks" in report.note


# ---------------------------------------------------------------------------------------
# 3. the daily report's single-day range
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("noise", ["additive", "proportional"])
def test_one_day_interval_covers_90_percent(noise):
    """Fit on 90 days, predict the 91st: the 90% range must cover 85-95% of such days. The
    multi-day G14 formula at m = 1 covered ~99%, and scaling sigma by max(1, level / mean)
    alone ~97% when the noise grows with runtime."""
    rng = np.random.default_rng(31 if noise == "additive" else 32)
    hits = n = 0
    rho = 0.3  # day-to-day correlation of the weather-free part
    for _ in range(1000):
        means = 70 + 8 * rng.standard_normal(91)
        e = np.empty(91)
        e[0] = rng.standard_normal()
        for i in range(1, 91):
            e[i] = rho * e[i - 1] + math.sqrt(1 - rho * rho) * rng.standard_normal()
        level = np.array([_true_runtime(m) + 2000.0 for m in means])
        sd = 1500.0 if noise == "additive" else 0.12 * level + 300.0
        y = np.maximum(0.0, level + sd * e)
        rows = [_row(date(2026, 5, 1) + timedelta(days=i), means[i], y[i]) for i in range(91)]
        fit = baseline.fit_baseline(rows[:90], "cool")
        if fit is None:
            continue
        exp = baseline.expected_seconds(fit, rows[90])
        hw = baseline.residual_stats([(fit, 1.0)]).day_halfwidth(exp)
        n += 1
        hits += abs(y[90] - exp) <= hw
    assert n >= 990
    assert 0.85 <= hits / n <= 0.95, hits / n


def test_daily_report_unit_range_is_the_single_day_interval():
    rng = np.random.default_rng(8)
    rows = _shoulder_days(rng, 91, date(2026, 6, 1), lambda i: 70 + 8 * math.sin(i))
    fit = baseline.fit_baseline(rows[:90], "cool")
    line = reports._unit_line(rows[90], {("up", "cool"): fit}, {("up", "cool")}, "up")
    exp = baseline.expected_covered_seconds(fit, rows[90])
    hw = baseline.residual_stats([(fit, 1.0)]).day_halfwidth(exp)
    assert line["baseline_ok"] and line["expected_min"] == pytest.approx(exp / 60, abs=0.05)
    assert line["ci90_min"] == [round(max(exp - hw, 0) / 60, 1), round((exp + hw) / 60, 1)]
    st = baseline.residual_stats([(fit, 1.0)])
    assert st.halfwidth(1, exp) > 1.2 * st.day_halfwidth()  # G14 stays for multi-day periods only


# ---------------------------------------------------------------------------------------
# 7. balance point: smallest intercept among candidates that pass NMBE
# ---------------------------------------------------------------------------------------


def test_balance_point_rule_prefers_a_candidate_that_passes_nmbe(monkeypatch):
    rng = np.random.default_rng(0)
    rows = _shoulder_days(rng, 90, date(2026, 3, 1), lambda i: 62 + 16 * i / 89 + 3 * rng.standard_normal())

    monkeypatch.setattr(baseline, "NMBE_MAX", math.inf)  # every candidate "passes": the old rule
    old = baseline.fit_baseline(rows, "cool")
    monkeypatch.setattr(baseline, "NMBE_MAX", -1.0)  # none passes: falls back to the old rule
    fallback = baseline.fit_baseline(rows, "cool")
    monkeypatch.undo()
    new = baseline.fit_baseline(rows, "cool")

    # The old pick had its intercept pinned at 0, so its residuals no longer average to zero.
    assert old.intercept_s == 0.0 and abs(old.nmbe) > baseline.NMBE_MAX and not old.passes
    assert new.passes and abs(new.nmbe) <= baseline.NMBE_MAX and new.intercept_s > 0
    assert new.balance_point_f == 65.0  # the true one, and inside the same 90% set
    assert "passes NMBE" in new.notes
    assert (fallback.balance_point_f, fallback.intercept_s) == (old.balance_point_f, old.intercept_s)
    assert "none passes NMBE" in fallback.notes


# ---------------------------------------------------------------------------------------
# 2. the in-progress day and partial days in /runtime/daily's helper
# ---------------------------------------------------------------------------------------


def _insert_runtime(db, t0: datetime, t1: datetime, unit: str, cool: int, out_f: float = 85.0) -> None:
    db.execute(
        text(
            """
            INSERT INTO runtime_5m (ts, unit_key, comp_cool1, fan, hvac_mode, zone_temp_f, outdoor_temp_f, source)
            SELECT t, :u, :c, :c, 'cool', 76, :o, 'test'
            FROM generate_series(CAST(:t0 AS timestamptz), CAST(:t1 AS timestamptz) - interval '5 minutes',
                                 interval '5 minutes') AS t
            """
        ),
        {"u": unit, "c": cool, "o": out_f, "t0": t0, "t1": t1},
    )


def test_daily_runtime_expectation_covers_the_same_slots_and_skips_today(db, monkeypatch):
    from climate.timeutil import day_bounds_utc

    z = ZoneInfo(TZ)
    today = date(2026, 7, 15)
    now = datetime(2026, 7, 15, 23, 0, tzinfo=z).astimezone(UTC)  # 23 of 24 hours in: >= 90%
    y0, y1 = day_bounds_utc(today - timedelta(days=1), TZ)
    _insert_runtime(db, y0 + timedelta(hours=1), y1, "up", 100)  # yesterday: first hour missing
    _insert_runtime(db, y1, now, "up", 100)
    db.flush()
    fit = baseline.BaselineFit(unit_key="up", mode="cool", balance_point_f=65, intercept_s=0, slope_s_per_dd=1000,
                               n_days=60, r2=0.9, cvrmse=0.1, nmbe=0.0, resid_std_s=300, resid_lag1=0.2,
                               train_start=today, train_end=today)
    monkeypatch.setattr(baseline, "active_fits", lambda s: {("up", "cool"): fit})
    out = {r.date: r for r in baseline.daily_runtime(db, days=2, now=now)}
    assert set(out) == {today - timedelta(days=1), today}
    yday = out[today - timedelta(days=1)]
    full = 1000 * 20.0  # 85°F all day = 20 CDD
    assert yday.cool_min == pytest.approx(100 * 276 / 60, abs=0.1)
    assert yday.expected_min == pytest.approx(full * 276 / 288 / 60, abs=0.1)  # same 276 slots
    assert out[today].expected_min is None  # in progress, even with 96% of its slots


# ---------------------------------------------------------------------------------------
# heating metric cache
# ---------------------------------------------------------------------------------------


def test_heat_metrics_is_cached_but_follows_unit_changes(db, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(daily, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    real_execute = db.execute

    def counting(stmt, *a, **kw):
        if stmt is daily._HEAT_SQL:
            calls.append(1)
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", counting)
    assert daily.heat_metrics(db) == {"main": False, "up": False, "bed": False}
    db.execute(text("INSERT INTO runtime_5m (ts, unit_key, comp_heat1, source) VALUES (now(), 'bed', 120, 'test')"))
    assert daily.heat_metrics(db)["bed"] is False and len(calls) == 1  # reused, no runtime_5m scan
    clock[0] += daily.HEAT_METRICS_TTL_S - 1
    assert daily.heat_metrics(db)["bed"] is False and len(calls) == 1
    clock[0] += 2  # 10 minutes on: the unit that now reports compressor heat is a heat pump
    assert daily.heat_metrics(db)["bed"] is True and len(calls) == 2
    db.execute(text("UPDATE units SET equipment = '{\"heating\": \"furnace\"}' WHERE key = 'bed'"))
    assert daily.heat_metrics(db)["bed"] is False and len(calls) == 3  # equipment edits apply at once

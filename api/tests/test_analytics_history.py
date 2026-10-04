"""Analytics on the factories' synthetic history (65°F balance point, known slope, known
main->upstairs coupling). One history is built per module; tests that change data do it
inside a savepoint and roll it back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.factories import BALANCE_F, COUPLING_S_PER_DEGF, SLOPE_S_PER_DD, make_history

TZ = "America/Chicago"
HISTORY_DAYS = 64


@pytest.fixture(scope="module")
def hist(database_url):
    from climate.store.db import _factory
    from tests.conftest import reset_db

    reset_db()
    s = _factory()()
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    make_history(s, days=HISTORY_DAYS, end=end)
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@contextmanager
def scratch(s) -> Iterator[None]:
    sp = s.begin_nested()
    try:
        yield
    finally:
        sp.rollback()


def _yesterday() -> date:
    from climate.timeutil import local_date, utcnow

    return local_date(utcnow(), TZ) - timedelta(days=1)


def _scale_cooling(s, start: date, end: date, factor: float, units: tuple[str, ...] = ("main", "up", "bed")) -> None:
    from climate.timeutil import day_bounds_utc

    s.execute(
        text("UPDATE runtime_5m SET comp_cool1 = round(comp_cool1 * :f) "
             "WHERE ts >= :a AND ts < :b AND unit_key = ANY(:u)"),
        {"f": factor, "a": day_bounds_utc(start, TZ)[0], "b": day_bounds_utc(end, TZ)[1], "u": list(units)},
    )


# ---------------------------------------------------------------------------------------
# daily rows and baselines
# ---------------------------------------------------------------------------------------


def test_daily_rows_from_history(hist):
    from climate.analytics.daily import daily_rows, degree_hours, is_complete
    from climate.timeutil import day_bounds_utc

    y = _yesterday()
    rows = daily_rows(hist, y - timedelta(days=6), y, TZ)
    assert len(rows) == 21
    assert [r.unit_key for r in rows[::7]] == ["main", "up", "bed"]
    for r in rows:
        assert is_complete(r) and r.mode == "cool" and r.weather_sources == ["open-meteo"]
        assert (r.heat_s, r.aux_s) == (0.0, 0.0)
        assert r.cdd65 == pytest.approx(degree_hours(r.hourly_outdoor_f, 65, "cool"), abs=1e-3)
    a, b = day_bounds_utc(y, TZ)
    direct = hist.execute(
        text("SELECT sum(comp_cool1) FROM runtime_5m WHERE unit_key = 'up' AND ts >= :a AND ts < :b"), {"a": a, "b": b}
    ).scalar()
    assert next(r for r in rows if r.unit_key == "up" and r.day == y).cool_s == direct


def test_baseline_recovers_factory_balance_point_and_slope(hist):
    """The bed wing's runtime is pure weather in the factories (0.8 x the slope, no coupling,
    no weekday effect), so it is the clean recovery check. The other two units carry
    weekday-only effects that the factories' exactly-weekly weather wave aliases with
    temperature; they must still fit, and every fit keeps its residual stats."""
    from climate.analytics.baseline import fit_window

    y = _yesterday()
    fits = fit_window(hist, y - timedelta(days=89), y, TZ)
    assert set(fits) == {("main", "cool"), ("up", "cool"), ("bed", "cool")}
    bed = fits[("bed", "cool")]
    assert abs(bed.balance_point_f - BALANCE_F) <= 3
    assert bed.slope_s_per_dd == pytest.approx(0.8 * SLOPE_S_PER_DD, rel=0.15)
    assert bed.intercept_s >= 0
    assert bed.passes, bed.check_summary()
    assert bed.n_days >= HISTORY_DAYS - 2
    for f in fits.values():
        assert f.intercept_s >= 0 and f.slope_s_per_dd > 0 and f.resid_std_s > 0
        assert -1 <= f.resid_lag1 <= 1 and f.daily and len(f.daily) == f.n_days


def test_refit_all_stores_and_retires(hist):
    from climate.analytics.baseline import active_fits, baselines_out, refit_all
    from climate.store.orm import Experiment, ExperimentDay
    from climate.timeutil import utcnow

    with scratch(hist):
        ids = refit_all(hist, utcnow())
        assert len(ids) == 3
        ids2 = refit_all(hist, utcnow())
        assert len(ids2) == 3 and not set(ids) & set(ids2)
        counts = dict(hist.execute(text("SELECT status, count(*) FROM model_fits WHERE kind = 'baseline' GROUP BY status")).all())
        assert counts == {"active": 3, "retired": 3}
        row = hist.execute(text("SELECT params, metrics FROM model_fits WHERE id = :i"), {"i": ids2[0]}).one()
        assert set(row.params) >= {"balance_point_f", "intercept_s", "slope_s_per_dd", "resid_std_s", "resid_lag1"}
        assert set(row.metrics) >= {"r2", "cvrmse", "nmbe", "n_days"}
        fits = active_fits(hist)
        assert {f.fit_id for f in fits.values()} == set(ids2)
        out = baselines_out(hist)
        assert [o.unit_key for o in out] == ["main", "up", "bed"]
        assert all(o.fitted_at is not None for o in out)
        n_before = fits[("bed", "cool")].n_days

        # Days in a running experiment's treatment arm never train a baseline.
        y = _yesterday()
        exp = Experiment(name="t", hypothesis="h", arms=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                         design={}, status="running", proposed_by="owner")
        hist.add(exp)
        hist.flush()
        for i in range(10):
            hist.add(ExperimentDay(experiment_id=exp.id, day=y - timedelta(days=i), arm="b" if i % 2 else "a"))
        hist.flush()
        refit_all(hist, utcnow())
        assert active_fits(hist)[("bed", "cool")].n_days == n_before - 5


# ---------------------------------------------------------------------------------------
# savings and the waterfall
# ---------------------------------------------------------------------------------------


def test_savings_interval_contains_zero_for_unchanged_behaviour(hist):
    from climate.analytics.attribution import savings

    y = _yesterday()
    s = savings(hist, y - timedelta(days=13), y)
    assert s.baseline_ok, s.note
    assert s.n_days == 14 and len(s.days) == 14
    assert s.ci90_low_pct is not None and s.ci90_high_pct is not None
    assert s.ci90_low_pct <= 0 <= s.ci90_high_pct, s.note
    assert s.expected_min > 0 and s.actual_min > 0
    assert s.savings_min == pytest.approx(s.expected_min - s.actual_min, abs=0.2)
    assert {u.unit_key for u in s.by_unit} == {"main", "up", "bed"}
    assert "no savings can be claimed" in s.note


def test_savings_detects_an_injected_cut(hist):
    from climate.analytics.attribution import savings

    y = _yesterday()
    start = y - timedelta(days=13)
    with scratch(hist):
        _scale_cooling(hist, start, y, 0.85)
        s = savings(hist, start, y)
    assert s.baseline_ok, s.note
    assert s.savings_pct == pytest.approx(15.0, abs=3.0)
    assert s.ci90_low_pct > 0 and s.ci90_low_pct <= 15.0 <= s.ci90_high_pct, s.note
    assert all(u.savings_pct == pytest.approx(15.0, abs=5.0) for u in s.by_unit)
    assert "less than the weather predicts" in s.note


def test_waterfall_sums_and_attributes_a_strategy_change(hist):
    from climate.analytics.attribution import waterfall

    y = _yesterday()
    this_monday = y + timedelta(days=1) - timedelta(days=(y + timedelta(days=1)).weekday())
    ws = this_monday - timedelta(days=7)

    def check(w):
        assert [i.label for i in w.items] == ["Last week", "Weather", "Strategy & other", "This week"]
        assert [i.kind for i in w.items] == ["total", "delta", "delta", "total"]
        last, weather, strategy, this = (i.minutes for i in w.items)
        assert last + weather + strategy == pytest.approx(this, abs=0.051)
        assert w.week_start == ws and w.prev_week_start == ws - timedelta(days=7)
        assert w.strategy_ci90_min is not None
        return strategy

    w = waterfall(hist, ws + timedelta(days=3))  # any day of the week maps to its Monday
    strategy = check(w)
    lo, hi = w.strategy_ci90_min
    assert lo <= strategy <= hi and lo <= 0 <= hi, w.note

    with scratch(hist):
        _scale_cooling(hist, ws, ws + timedelta(days=6), 0.85)
        w2 = waterfall(hist, ws)
    strategy2 = check(w2)
    assert w2.strategy_ci90_min[1] < 0, w2.note
    assert w2.items[1].minutes == pytest.approx(w.items[1].minutes, abs=0.1)  # weather didn't change
    assert strategy2 == pytest.approx(-0.15 * w.items[3].minutes + strategy, rel=0.05)


# ---------------------------------------------------------------------------------------
# coupling and natural experiments
# ---------------------------------------------------------------------------------------


def test_coupling_is_positive_and_the_bed_wing_placebo_is_not(hist):
    from climate.analytics.coupling import coupling

    c = coupling(hist, days=30)
    assert c.coef_min_per_degf is not None, c.interpretation
    true = 12 * COUPLING_S_PER_DEGF / 60  # minutes per hour per °F
    assert c.ci90[0] > 0 and c.ci90[0] <= c.coef_min_per_degf <= c.ci90[1]
    assert c.coef_min_per_degf == pytest.approx(true, rel=0.25)
    assert c.placebo_coef is not None and abs(c.placebo_coef) < 0.2 * c.coef_min_per_degf
    assert c.placebo_ci90[0] <= 0 <= c.placebo_ci90[1], c.interpretation
    assert 0 < len(c.points) <= 2000 and c.n_hours >= 24 * 25
    assert "consistent with no effect" in c.interpretation


def test_natural_experiments_find_the_float_effect(hist):
    from climate.analytics.coupling import natural_experiments

    ne = natural_experiments(hist, days=90)
    assert ne.estimate_min_per_event is not None, ne.note
    assert ne.ci90[0] > 0, ne.note
    assert ne.events and all(e.main_floor_float_f == pytest.approx(3.5, abs=0.5) for e in ne.events)
    assert all(e.expected_up_runtime_min is not None for e in ne.events)
    assert ne.placebo_estimate is not None and abs(ne.placebo_estimate) < 0.3 * ne.estimate_min_per_event
    assert ne.bed_wing_estimate is not None and abs(ne.bed_wing_estimate) < 0.3 * ne.estimate_min_per_event


# ---------------------------------------------------------------------------------------
# drift, report, wire helpers
# ---------------------------------------------------------------------------------------


def test_drift_flags_a_shift_but_not_steady_behaviour(hist):
    from climate.analytics.baseline import refit_all
    from climate.analytics.metrics import drift
    from climate.timeutil import utcnow

    y = _yesterday()
    with scratch(hist):
        assert drift(hist).units == []  # no active baselines yet
        refit_all(hist, utcnow())
        steady = drift(hist)
        assert {(u.unit_key, u.mode) for u in steady.units} == {("main", "cool"), ("up", "cool"), ("bed", "cool")}
        assert not any(u.drifting for u in steady.units), steady.note
        _scale_cooling(hist, y - timedelta(days=6), y, 0.75)
        shifted = drift(hist)
    assert all(u.drifting and u.z < 0 and u.resid_mean_pct < -15 for u in shifted.units), shifted.note
    assert "Drifting" in shifted.note


def test_daily_report_is_idempotent(hist):
    from climate.analytics.reports import build_daily_report
    from climate.store.orm import Alert, ControlAction
    from climate.timeutil import day_bounds_utc

    y = _yesterday()
    with scratch(hist):
        hist.add(ControlAction(ts=day_bounds_utc(y, TZ)[0] + timedelta(hours=14), unit_key="main", actor="controller",
                               mode="suggest", channel="none", action="set_hold", status="suggested", reason="test"))
        hist.add(Alert(level="warn", kind="sensor_offline", title="Office sensor offline"))
        hist.flush()
        rid = build_daily_report(hist, y)
        first = hist.execute(text("SELECT body_md, data FROM reports WHERE id = :i"), {"i": rid}).one()
        rid2 = build_daily_report(hist, y)
        assert rid2 == rid
        n = hist.execute(text("SELECT count(*) FROM reports WHERE kind = 'daily' AND period_start = :d"), {"d": y}).scalar()
        assert n == 1
        again = hist.execute(text("SELECT body_md, data FROM reports WHERE id = :i"), {"i": rid}).one()
        assert again.body_md == first.body_md and again.data == first.data
        body, data = first.body_md, first.data
        assert "Weather data by Open-Meteo.com" in body and data["open_meteo"] is True
        assert [u["unit_key"] for u in data["units"]] == ["main", "up", "bed"]
        for u in data["units"]:
            assert u["actual_min"] > 0 and u["expected_min"] > 0 and u["ci90_min"][0] < u["expected_min"] < u["ci90_min"][1]
        assert data["actions"] == {"suggested": 1}
        assert "Office sensor offline" in body and "Open alerts (1)" in body
        assert data["house"]["baseline_ok"] is True
        empty_id = build_daily_report(hist, y - timedelta(days=200))
        empty = hist.execute(text("SELECT body_md, data FROM reports WHERE id = :i"), {"i": empty_id}).one()
        assert "No runtime data" in empty.body_md and "Open-Meteo" not in empty.body_md


def test_daily_runtime_and_unit_today_on_history(hist):
    from climate.analytics.baseline import daily_runtime, refit_all
    from climate.analytics.metrics import unit_today
    from climate.timeutil import local_date, utcnow

    with scratch(hist):
        refit_all(hist, utcnow())
        out = daily_runtime(hist, days=7)
    today = local_date(utcnow(), TZ)
    assert len(out) in (18, 21)  # today is present unless the history ends before its first slot
    for r in out:
        if r.date == today:
            assert r.expected_min is None  # partial day: no expectation
        else:
            assert r.expected_min is not None and r.expected_min == pytest.approx(r.cool_min, rel=0.3)
    live = unit_today(hist, utcnow(), TZ)
    assert set(live) == {"main", "up", "bed"}
    assert all(v["today_runtime_min"] >= 0 for v in live.values())

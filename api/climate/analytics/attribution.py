"""Separate weather from strategy: avoided-runtime savings and the weekly waterfall.

Both use an out-of-sample baseline: the per-unit fits trained on the 90 days BEFORE the
period being judged (``baseline.pre_period_fits``), never a fit that has already seen the
days it is judging. Totals are stage-1 runtime, weighted by ``units.power_weight`` (plain
minutes while every weight is 1.0).

Which unit/modes count: a mode is "involved" for a unit when its actual runtime, or its
baseline's degree-day-driven expectation, is at least 5% of that unit's runtime in the
period. An involved mode without a baseline, or with one failing the ASHRAE G14 checks,
blocks every claim (baseline_ok = False). Minor runtime in a mode without a baseline is
left out of both sides and the note says how much.

Days count only when every unit with data in the period has a complete day
(``daily.is_complete``); a complete day's expectation is scaled to its coverage.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from climate.analytics.baseline import (
    MIN_DAYS,
    MIN_RUNTIME_DAYS,
    MODES,
    TRAIN_DAYS,
    BaselineFit,
    ResidualStats,
    expected_covered_seconds,
    involved_modes,
    pre_period_fits,
    residual_stats,
)
from climate.analytics.daily import (
    DayRow,
    daily_rows,
    house_tz,
    is_complete,
    mode_seconds,
    unit_order,
    unit_weights,
)
from climate.api.schemas import Savings, SavingsByUnit, SavingsDay, Waterfall, WaterfallItem

MODE_WORD = {"cool": "cooling", "heat": "heating"}
UNIT_NAME = {"main": "main floor", "up": "upstairs", "bed": "bed/office wing"}


def _uname(u: str) -> str:
    return UNIT_NAME.get(u, u)


def _pct(x: float) -> str:
    return f"{x:.0f}%" if abs(x) >= 9.5 else f"{x:.1f}%"


@dataclass
class _Period:
    """One judged period: what was compared, on which days, and why not (if not)."""

    involved: set[tuple[str, str]] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)  # units with data in the period
    days: list[date] = field(default_factory=list)  # usable days
    skipped_days: list[date] = field(default_factory=list)
    actual: dict[date, float] = field(default_factory=dict)  # weighted seconds per day
    expected: dict[date, float] = field(default_factory=dict)  # weighted seconds per day
    unit_actual: dict[str, float] = field(default_factory=lambda: defaultdict(float))  # unweighted
    unit_expected: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    outdoor: dict[date, float | None] = field(default_factory=dict)
    left_out_s: float = 0.0  # minor runtime without a baseline, excluded from both sides

    @property
    def ok(self) -> bool:
        return not self.problems


def _judge(
    rows: list[DayRow], fits: dict[tuple[str, str], BaselineFit], weights: dict[str, float],
    involved: set[tuple[str, str]], days: list[date] | None = None,
) -> _Period:
    """Actual vs expected per usable day. ``days`` restricts the candidate days."""
    p = _Period(involved=involved)
    by_day: dict[date, dict[str, DayRow]] = defaultdict(dict)
    for r in rows:
        by_day[r.day][r.unit_key] = r
    p.units = sorted({r.unit_key for r in rows}, key=unit_order)
    for u, m in sorted(involved, key=lambda k: (unit_order(k[0]), k[1])):
        fit = fits.get((u, m))
        if fit is None:
            p.problems.append(
                f"no {MODE_WORD[m]} baseline for the {_uname(u)} yet (needs {MIN_DAYS} complete days with at least "
                f"{MIN_RUNTIME_DAYS} days of {MODE_WORD[m]} in the {TRAIN_DAYS} days before the period)"
            )
        elif not fit.passes:
            p.problems.append(f"the {_uname(u)} {MODE_WORD[m]} baseline fails its checks ({fit.check_summary()})")

    candidates = sorted(by_day) if days is None else sorted(d for d in days if d in by_day)
    for d in candidates:
        units = by_day[d]
        if any(u not in units or not is_complete(units[u]) for u in p.units):
            p.skipped_days.append(d)
            continue
        a_day = e_day = 0.0
        usable = True
        for u in p.units:
            r, w = units[u], weights.get(u, 1.0)
            a_u = e_u = 0.0
            for m in MODES:
                if (u, m) not in involved:
                    p.left_out_s += mode_seconds(r, m)
                    continue
                a_u += mode_seconds(r, m)
                fit = fits.get((u, m))
                if fit is not None:
                    e = expected_covered_seconds(fit, r)
                    if math.isnan(e):
                        usable = False
                    e_u += e
            if not usable:
                break
            p.unit_actual[u] += a_u
            p.unit_expected[u] += e_u
            a_day += w * a_u
            e_day += w * e_u
        if not usable:
            p.skipped_days.append(d)
            continue
        p.days.append(d)
        p.actual[d] = a_day
        p.expected[d] = e_day
        means = [units[u].outdoor_mean_f for u in p.units if units[u].outdoor_mean_f is not None]
        p.outdoor[d] = means[0] if means else None
    return p


def _stats(p: _Period, fits: dict[tuple[str, str], BaselineFit], weights: dict[str, float]) -> ResidualStats | None:
    return residual_stats((fits[k], weights.get(k[0], 1.0)) for k in sorted(p.involved) if k in fits)


def savings(session: Session, start: date, end: date) -> Savings:
    """Savings = expected - actual (IPMVP avoided energy) on total-house stage-1 runtime,
    weighted by units.power_weight. 90% interval from the baseline residual std with the
    lag-1 autocorrelation adjustment (ASHRAE G14 fractional savings uncertainty). If any
    involved baseline fails its checks, baseline_ok = False and savings fields are None."""
    if end < start:
        raise ValueError("end is before start")
    tz = house_tz(session)
    fits = pre_period_fits(session, start, tz)
    rows = daily_rows(session, start, end, tz)
    weights = unit_weights(session)
    p = _judge(rows, fits, weights, involved_modes(rows, fits))

    actual_s = sum(p.actual.values())
    n = len(p.days)
    ok = p.ok and bool(p.involved)
    by_unit = [
        SavingsByUnit(
            unit_key=u,
            expected_min=round(p.unit_expected[u] / 60.0, 1) if ok and n else None,
            actual_min=round(p.unit_actual[u] / 60.0, 1),
            savings_pct=(
                round((p.unit_expected[u] - p.unit_actual[u]) / p.unit_expected[u] * 100.0, 1)
                if ok and n and p.unit_expected[u] > 0 else None
            ),
        )
        for u in p.units
    ]
    days = [
        SavingsDay(
            Date=d,
            expected_min=round(p.expected[d] / 60.0, 1) if ok else None,
            actual_min=round(p.actual[d] / 60.0, 1),
            outdoor_mean_f=p.outdoor.get(d),
        )
        for d in p.days
    ]
    base = {"start": start, "end": end, "n_days": n, "actual_min": round(actual_s / 60.0, 1), "by_unit": by_unit,
            "days": days}
    skipped = f" {len(p.skipped_days)} day(s) without complete data for every unit were left out." if p.skipped_days else ""

    if not p.involved:
        return Savings(**base, expected_min=None, savings_min=None, savings_pct=None, ci90_low_pct=None,
                       ci90_high_pct=None, baseline_ok=False,
                       note="No savings claim: there is no heating or cooling runtime in this period and no baseline "
                            "that expects any." + skipped)
    if not p.ok:
        why = "; ".join(p.problems)
        return Savings(**base, expected_min=None, savings_min=None, savings_pct=None, ci90_low_pct=None,
                       ci90_high_pct=None, baseline_ok=False,
                       note=f"No savings claim yet: {why}. Savings need a passing weather baseline for every unit and "
                            f"mode that ran." + skipped)
    expected_s = sum(p.expected.values())
    if n == 0 or expected_s <= 0:
        return Savings(**base, expected_min=None, savings_min=None, savings_pct=None, ci90_low_pct=None,
                       ci90_high_pct=None, baseline_ok=True,
                       note="Not enough data: no day in this period has complete runtime and weather data for every "
                            "unit." + skipped)

    saved = expected_s - actual_s
    pct = saved / expected_s * 100.0
    st = _stats(p, fits, weights)
    hw = st.halfwidth(n, expected_s / n) if st else math.inf
    lo, hi = (saved - hw) / expected_s * 100.0, (saved + hw) / expected_s * 100.0
    interval = f"90% interval {_pct(lo)} to {_pct(hi)}"
    if lo > 0:
        note = f"Over {n} days the house ran {_pct(pct)} less than the weather predicts ({interval})."
    elif hi < 0:
        note = f"Over {n} days the house ran {_pct(-pct)} more than the weather predicts ({interval})."
    else:
        note = (f"Over {n} days runtime was within what the weather explains ({_pct(pct)}, {interval}), so no "
                f"savings can be claimed.")
    note += f" Baseline: the {TRAIN_DAYS} days before {start.isoformat()}."
    if p.left_out_s > 0:
        note += f" {p.left_out_s / 60.0:.0f} min of minor runtime without a baseline were left out."
    note += skipped
    return Savings(**base, expected_min=round(expected_s / 60.0, 1), savings_min=round(saved / 60.0, 1),
                   savings_pct=round(pct, 2), ci90_low_pct=round(lo, 2), ci90_high_pct=round(hi, 2),
                   baseline_ok=True, note=note)


def waterfall(session: Session, week_start: date) -> Waterfall:
    """Last week total -> weather effect (expected this week - expected last week) ->
    strategy & other (the rest) -> this week total, with a 90% interval on the strategy bar.

    Weeks start on Monday (another date is moved back to its Monday). Both weeks use the
    same baseline, trained on the 90 days before last week. Only weekdays complete in BOTH
    weeks are compared, so a week in progress is compared with the same days of last week."""
    ws = week_start - timedelta(days=week_start.weekday())
    prev = ws - timedelta(days=7)
    tz = house_tz(session)
    fits = pre_period_fits(session, prev, tz)
    weights = unit_weights(session)
    rows_this = daily_rows(session, ws, ws + timedelta(days=6), tz)
    rows_last = daily_rows(session, prev, prev + timedelta(days=6), tz)
    involved = involved_modes(rows_this + rows_last, fits)
    this = _judge(rows_this, fits, weights, involved)
    last = _judge(rows_last, fits, weights, involved)
    offsets = sorted({(d - ws).days for d in this.days} & {(d - prev).days for d in last.days})
    this = _judge(rows_this, fits, weights, involved, [ws + timedelta(days=o) for o in offsets])
    last = _judge(rows_last, fits, weights, involved, [prev + timedelta(days=o) for o in offsets])

    a_this, a_last = sum(this.actual.values()) / 60.0, sum(last.actual.values()) / 60.0
    m = len(offsets)
    partial = "" if m == 7 else f" Compared on the {m} weekday(s) with complete data in both weeks."
    totals_only = [
        WaterfallItem(label="Last week", minutes=round(a_last, 1), kind="total"),
        WaterfallItem(label="This week", minutes=round(a_this, 1), kind="total"),
    ]
    if m == 0:
        return Waterfall(week_start=ws, prev_week_start=prev, items=totals_only, strategy_ci90_min=None,
                         note="Not enough data: no weekday has complete data for every unit in both weeks.")
    problems = list(dict.fromkeys(this.problems + last.problems))
    if problems or not involved:
        why = "; ".join(problems) if problems else "no heating or cooling runtime in either week"
        return Waterfall(week_start=ws, prev_week_start=prev, items=totals_only, strategy_ci90_min=None,
                         note=f"Weather and strategy can't be separated yet: {why}." + partial)

    e_this, e_last = sum(this.expected.values()) / 60.0, sum(last.expected.values()) / 60.0
    weather = e_this - e_last
    strategy = (a_this - a_last) - weather
    st = _stats(this, fits, weights)
    ci = None
    if st is not None:
        hw = math.hypot(st.halfwidth(m, e_this * 60.0 / m), st.halfwidth(m, e_last * 60.0 / m)) / 60.0
        ci = (round(strategy - hw, 1), round(strategy + hw, 1))
    # Round the bars so they still add up exactly: strategy takes the rounding remainder.
    r_last, r_this, r_weather = round(a_last, 1), round(a_this, 1), round(weather, 1)
    items = [
        WaterfallItem(label="Last week", minutes=r_last, kind="total"),
        WaterfallItem(label="Weather", minutes=r_weather, kind="delta"),
        WaterfallItem(label="Strategy & other", minutes=round(r_this - r_last - r_weather, 1), kind="delta"),
        WaterfallItem(label="This week", minutes=r_this, kind="total"),
    ]
    w_word = "added" if weather >= 0 else "saved"
    note = f"Weather {w_word} {abs(weather):.0f} min versus last week."
    if ci is not None and (ci[0] > 0 or ci[1] < 0):
        s_word = "added" if strategy > 0 else "saved"
        note += f" Strategy & other {s_word} {abs(strategy):.0f} min (90% interval {ci[0]:.0f} to {ci[1]:.0f})."
    elif ci is not None:
        note += (f" Strategy & other: {strategy:+.0f} min, within the noise (90% interval {ci[0]:.0f} to "
                 f"{ci[1]:.0f}).")
    return Waterfall(week_start=ws, prev_week_start=prev, items=items, strategy_ci90_min=ci, note=note + partial)

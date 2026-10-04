"""Experiment analysis and power, on weather-normalized daily total-house runtime.

The metric is the daily house residual: actual stage-1 runtime summed over the units (weighted
by ``units.power_weight``) minus what each unit's weather baseline expected for that day's mode.
Weather is thereby removed day by day, and the switchback's randomization handles everything
else. The effect is reported as the difference in mean residual (treatment arm minus the first,
control arm) as a percentage of mean expected runtime: negative means the treatment used LESS
runtime than the control under the same weather.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from scipy import stats
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from climate.analytics import baseline as baseline_mod
from climate.analytics import daily as daily_mod
from climate.api.schemas import Checkpoint, ExperimentAnalysis, PowerOut
from climate.store.app_settings import LocationSettings, get_setting
from climate.store.orm import Experiment, ExperimentDay, Unit
from climate.timeutil import SLOT, day_bounds_utc, local_date, utcnow

MIN_DATA_FRACTION = 0.90  # a day needs >= 90% of its 5-minute slots for every unit
MIXED_MODE_FRACTION = 0.10  # minority mode above 10% of runtime -> a mixed day, excluded
RECENT_REFILL_DAYS = 3  # late runtime reports: recompute the last few finished days nightly
INFORMATIONAL_ALPHA = 0.10  # 90% interval between checkpoints (shown, never acted on)
_DEFAULT_LOOKS_FOR_POWER = 3


# ---------------------------------------------------------------------------------------
# helpers shared with climate.models.backtest
# ---------------------------------------------------------------------------------------


def unit_weights(session: Session) -> dict[str, float]:
    return {u.key: float(u.power_weight if u.power_weight is not None else 1.0)
            for u in session.scalars(select(Unit).order_by(Unit.sort)).all()}


def pooled_cv(fits: dict[tuple[str, str], Any], weights: dict[str, float],
              mode: str | None = None) -> tuple[float | None, float, str | None]:
    """(house residual CV, lag-1 autocorrelation, mode) from the active baselines.

    The house CV is the expected-runtime-weighted average of the units' CV(RMSE), i.e. the
    house total's CV if the units' daily errors move together (they share the weather and the
    family's schedule), which is the conservative choice. When ``mode`` is None the mode with
    the most expected runtime is used. Returns (None, 0, None) without usable fits."""
    best: tuple[float | None, float, str | None] = (None, 0.0, None)
    best_mean = -1.0
    for m in (("cool", "heat") if mode is None else (mode,)):
        num = den = rho_num = 0.0
        for (unit, fmode), fit in fits.items():
            if fmode != m or unit not in weights:
                continue
            cv = float(getattr(fit, "cvrmse", 0.0) or 0.0)
            sd = float(getattr(fit, "resid_std_s", 0.0) or 0.0)
            if cv <= 0 or sd <= 0:
                continue
            mean = sd / cv
            w = weights[unit] * mean
            num += w * cv
            den += w
            rho_num += w * float(getattr(fit, "resid_lag1", 0.0) or 0.0)
        if den > 0 and den > best_mean:
            best_mean = den
            rho = min(0.9, max(0.0, rho_num / den))
            best = (num / den, rho, m)
    return best


@dataclass
class HouseDay:
    day: date
    actual_s: float | None
    expected_s: float | None
    included: bool
    note: str | None


def _expected_slots(d: date, tz: str) -> int:
    start, end = day_bounds_utc(d, tz)
    return int((end - start) / SLOT)


def house_days(session: Session, start: date, end: date, tz: str) -> dict[date, HouseDay]:
    """Weighted house actual / expected stage-1 seconds per local day in [start, end]."""
    weights = unit_weights(session)
    units = list(weights)
    rows = daily_mod.daily_rows(session, start, end, tz, units)
    fits = baseline_mod.active_fits(session)
    by_day: dict[date, dict[str, Any]] = {}
    for r in rows:
        by_day.setdefault(r.day, {})[r.unit_key] = r
    out: dict[date, HouseDay] = {}
    d = start
    while d <= end:
        out[d] = _house_day(d, by_day.get(d, {}), units, weights, fits, tz)
        d += timedelta(days=1)
    return out


def _house_day(d: date, rows: dict[str, Any], units: list[str], weights: dict[str, float],
               fits: dict[tuple[str, str], Any], tz: str) -> HouseDay:
    missing = [u for u in units if u not in rows]
    if missing:
        return HouseDay(d, None, None, False, f"no runtime data for {', '.join(missing)}")
    need = MIN_DATA_FRACTION * _expected_slots(d, tz)
    thin = [u for u in units if rows[u].slots < need]
    cool = sum(weights[u] * float(rows[u].cool_s or 0.0) for u in units)
    heat = sum(weights[u] * float(rows[u].heat_s or 0.0) for u in units)
    if cool > 0 and heat > 0 and min(cool, heat) > MIXED_MODE_FRACTION * (cool + heat):
        mode, mixed = ("cool" if cool >= heat else "heat"), True
    else:
        mixed = False
        if cool > 0 or heat > 0:
            mode = "cool" if cool >= heat else "heat"
        else:
            temps = [r.outdoor_mean_f for r in rows.values() if r.outdoor_mean_f is not None]
            mode = "cool" if temps and float(np.mean(temps)) >= 65.0 else "heat"
    actual = cool if mode == "cool" else heat
    expected = 0.0
    no_fit: list[str] = []
    for u in units:
        fit = fits.get((u, mode))
        if fit is None:
            no_fit.append(u)
            continue
        expected += weights[u] * float(baseline_mod.expected_seconds(fit, rows[u]))
    notes: list[str] = []
    if thin:
        notes.append(f"under 90% of the day's data for {', '.join(thin)}")
    if mixed:
        notes.append("mixed heating and cooling day")
    if no_fit:
        notes.append(f"no active {mode} baseline for {', '.join(no_fit)}")
    included = not notes
    return HouseDay(d, actual, None if no_fit else expected, included, "; ".join(notes) or None)


# ---------------------------------------------------------------------------------------
# filling experiment days
# ---------------------------------------------------------------------------------------


def _tz(session: Session) -> str:
    return get_setting(session, "location", LocationSettings).tz


def update_days(session: Session, now: datetime) -> None:
    """Fill actual_s / expected_s / residual_s on experiment_days for finished local days.

    Rows never processed are filled once; the last few finished days are recomputed every
    run so late runtime reports are picked up. ``included`` is False (with a note saying why)
    when any unit lacks a baseline for the day's mode, has < 90% of the day's data, or the
    house both heated and cooled that day."""
    tz = _tz(session)
    today = local_date(now, tz)
    recent = today - timedelta(days=RECENT_REFILL_DAYS)
    rows = session.scalars(
        select(ExperimentDay)
        .join(Experiment, Experiment.id == ExperimentDay.experiment_id)
        .where(
            Experiment.status.in_(("approved", "running", "completed", "stopped")),
            ExperimentDay.day < today,
            or_(ExperimentDay.day >= recent,
                and_(ExperimentDay.actual_s.is_(None), ExperimentDay.note.is_(None))),
        )
    ).all()
    if not rows:
        return
    hd = house_days(session, min(r.day for r in rows), max(r.day for r in rows), tz)
    for r in rows:
        h = hd[r.day]
        r.actual_s = h.actual_s
        r.expected_s = h.expected_s
        r.residual_s = (h.actual_s - h.expected_s) if h.actual_s is not None and h.expected_s is not None else None
        r.included = bool(h.included and r.residual_s is not None)
        r.note = h.note
    session.flush()


# ---------------------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------------------


@dataclass
class _Look:
    n_a: int
    n_b: int
    effect_pct: float
    lo_pct: float
    hi_pct: float
    level: float


def _block_design_effect(rho: float, block_days: int) -> float:
    """Variance factor for an arm mean built from blocks of ``block_days`` consecutive days
    whose residuals follow AR(1) with lag-1 correlation rho."""
    b = max(1, int(block_days))
    if rho <= 0 or b == 1:
        return 1.0
    return 1.0 + 2.0 / b * sum((b - j) * rho**j for j in range(1, b))


def _welch(res_a: list[float], res_b: list[float], expected: list[float], z_crit: float,
           deff: float) -> _Look | None:
    """Welch interval for mean(B) - mean(A) at the two-sided nominal level of ``z_crit``,
    using the Welch-Satterthwaite degrees of freedom, widened by the block design effect."""
    n_a, n_b = len(res_a), len(res_b)
    if n_a < 2 or n_b < 2:
        return None
    mean_exp = float(np.mean(expected)) if expected else 0.0
    if mean_exp <= 0:
        return None
    va, vb = float(np.var(res_a, ddof=1)), float(np.var(res_b, ddof=1))
    se2 = va / n_a + vb / n_b
    diff = float(np.mean(res_b) - np.mean(res_a))
    p_two = 2.0 * stats.norm.sf(z_crit)
    if se2 <= 0:
        half = 0.0
    else:
        denom = (va / n_a) ** 2 / (n_a - 1) + (vb / n_b) ** 2 / (n_b - 1)
        df = se2**2 / denom if denom > 0 else float(n_a + n_b - 2)
        t_crit = float(stats.t.isf(p_two / 2.0, df))
        half = t_crit * math.sqrt(se2 * deff)
    scale = 100.0 / mean_exp
    return _Look(n_a, n_b, diff * scale, (diff - half) * scale, (diff + half) * scale, 1.0 - p_two)


def _fmt_pct(x: float) -> str:
    return f"{x:+.1f}%"


def analyze(session: Session, experiment: Experiment) -> ExperimentAnalysis:
    """Effect = mean residual(arm B) - mean residual(arm A), as % of mean expected runtime;
    Welch interval at the checkpoint's alpha (z_crit) or, between checkpoints, a 90%
    interval labelled informational. Decisions are made only at pre-planned checkpoints, each
    using exactly the days up to that checkpoint: 'stop_win' when the interval excludes 0 in
    the treatment's favour; at the last look otherwise 'stop_futile' (the treatment did no
    better) or 'inconclusive' (it leaned better but did not clear the bar); 'continue'
    between looks."""
    tz = _tz(session)
    today = local_date(utcnow(), tz)
    design = dict(experiment.design or {})
    arms = [a["key"] for a in experiment.arms]
    labels = {a["key"]: a.get("label") or a["key"] for a in experiment.arms}
    arm_a, arm_b = arms[0], arms[1]
    checkpoints = [Checkpoint.model_validate(c) for c in design.get("checkpoints", [])]
    days = session.scalars(
        select(ExperimentDay).where(ExperimentDay.experiment_id == experiment.id).order_by(ExperimentDay.day)
    ).all()
    observed = [d for d in days if d.included and d.residual_s is not None and d.day < today]

    def none(decision: str, note: str, reached: int | None = None) -> ExperimentAnalysis:
        return ExperimentAnalysis(days_observed=len(observed), effect_pct=None, ci_low_pct=None,
                                  ci_high_pct=None, checkpoint_reached=reached, decision=decision,  # type: ignore[arg-type]
                                  note=note)

    if experiment.status in ("proposed", "rejected"):
        return none("not_started", "Not approved yet." if experiment.status == "proposed"
                    else "Rejected; it never ran.")
    start = experiment.start_date
    if start is None or today <= start:
        if experiment.status == "stopped":
            return none("inconclusive", "Stopped before its first day; no data.")
        when = f" It starts {start.isoformat()}." if start else ""
        return none("not_started", f"No finished days yet.{when}")

    finished = [d for d in days if d.day < today]
    elapsed = len(finished)
    fits = baseline_mod.active_fits(session)
    _, rho, _ = pooled_cv(fits, unit_weights(session))
    deff = _block_design_effect(rho, int(design.get("block_days", 1)))
    corr_note = (f" Intervals are widened for day-to-day correlation within blocks (lag-1 ≈ {rho:.2f})."
                 if deff > 1.0 else "")

    def look(cutoff: date | None, z_crit: float) -> _Look | None:
        sel = [d for d in observed if cutoff is None or d.day < cutoff]
        res_a = [float(d.residual_s) for d in sel if d.arm == arm_a]  # type: ignore[arg-type]
        res_b = [float(d.residual_s) for d in sel if d.arm == arm_b]  # type: ignore[arg-type]
        expected = [float(d.expected_s) for d in sel if d.arm in (arm_a, arm_b) and d.expected_s is not None]
        return _welch(res_a, res_b, expected, z_crit, deff)

    def describe(lk: _Look) -> str:
        direction = "less" if lk.effect_pct < 0 else "more"
        return (f"'{labels[arm_b]}' used {abs(lk.effect_pct):.1f}% {direction} weather-normalized runtime "
                f"than '{labels[arm_a]}' ({lk.n_b} vs {lk.n_a} days; {lk.level * 100:.1f}% interval "
                f"{_fmt_pct(lk.lo_pct)} to {_fmt_pct(lk.hi_pct)}).")

    def third_arm_note(cutoff: date | None) -> str:
        if len(arms) < 3:
            return ""
        arm_c = arms[2]
        sel = [d for d in observed if cutoff is None or d.day < cutoff]
        res_a = [float(d.residual_s) for d in sel if d.arm == arm_a]  # type: ignore[arg-type]
        res_c = [float(d.residual_s) for d in sel if d.arm == arm_c]  # type: ignore[arg-type]
        exp = [float(d.expected_s) for d in sel if d.arm in (arm_a, arm_c) and d.expected_s is not None]
        lk = _welch(res_a, res_c, exp, stats.norm.isf(INFORMATIONAL_ALPHA / 2), deff)
        if lk is None:
            return ""
        return (f" Secondary arm '{labels[arm_c]}' vs '{labels[arm_a]}': {_fmt_pct(lk.effect_pct)} "
                f"(90% {_fmt_pct(lk.lo_pct)} to {_fmt_pct(lk.hi_pct)}; not part of the pre-planned "
                "decision, treat it as a lead).")

    reached = [(i, cp) for i, cp in enumerate(checkpoints, start=1) if cp.day <= elapsed]
    n_cp = len(checkpoints)
    for i, cp in reached:
        cutoff = start + timedelta(days=cp.day)
        lk = look(cutoff, cp.z_crit)
        at = f"Checkpoint {i} of {n_cp} (day {cp.day})"
        if lk is not None and lk.hi_pct < 0:
            return ExperimentAnalysis(
                days_observed=len(observed), effect_pct=lk.effect_pct, ci_low_pct=lk.lo_pct, ci_high_pct=lk.hi_pct,
                checkpoint_reached=i, decision="stop_win",
                note=f"{at}: {describe(lk)} The whole interval is below zero, which clears this checkpoint's "
                     f"pre-planned bar: the treatment wins; stop and adopt it.{corr_note}{third_arm_note(cutoff)}")
        if i == n_cp:
            if lk is None:
                return none("inconclusive", f"{at}: too few usable days per arm to judge "
                            f"({len(observed)} usable of {elapsed}).", i)
            if lk.effect_pct >= 0 or lk.lo_pct > 0:
                decision, verdict = "stop_futile", (
                    "The treatment did no better than the control, so it is not worth adopting.")
            else:
                decision, verdict = "inconclusive", (
                    "It leaned toward the treatment but the interval still includes zero; a real effect "
                    "this small needs months of days, so judge it in the simulator instead.")
            return ExperimentAnalysis(
                days_observed=len(observed), effect_pct=lk.effect_pct, ci_low_pct=lk.lo_pct, ci_high_pct=lk.hi_pct,
                checkpoint_reached=i, decision=decision,  # type: ignore[arg-type]
                note=f"{at}, the final look: {describe(lk)} {verdict}{corr_note}{third_arm_note(cutoff)}")

    last_reached = reached[-1][0] if reached else None
    if experiment.status == "stopped":
        decision = "inconclusive"
        lead = "Stopped by the owner before its final checkpoint, so no pre-planned verdict exists. "
    else:
        decision = "continue"
        nxt = next((cp for cp in checkpoints if cp.day > elapsed), None)
        lead = (f"Day {elapsed} of {design.get('n_days', '?')}; next checkpoint on day {nxt.day}. "
                if nxt else f"Day {elapsed}. ")
        if reached:
            i, cp = reached[-1]
            lk = look(start + timedelta(days=cp.day), cp.z_crit)
            if lk is not None and cp.day == elapsed:
                # Exactly at an interim look: report the formal checkpoint interval.
                worse = (" The treatment used significantly MORE runtime; consider stopping it."
                         if lk.lo_pct > 0 else "")
                return ExperimentAnalysis(
                    days_observed=len(observed), effect_pct=lk.effect_pct, ci_low_pct=lk.lo_pct,
                    ci_high_pct=lk.hi_pct, checkpoint_reached=i, decision="continue",
                    note=f"Checkpoint {i} of {n_cp} (day {cp.day}): {describe(lk)} The interval includes zero "
                         f"or favours the control, so it does not clear this checkpoint's bar; the test "
                         f"continues to day {nxt.day if nxt else cp.day}.{worse}{corr_note}"
                         f"{third_arm_note(start + timedelta(days=cp.day))}")
            if lk is not None and lk.lo_pct > 0:
                lead += (f"At checkpoint {i} the treatment used significantly MORE runtime "
                         f"({_fmt_pct(lk.effect_pct)}); consider stopping it. ")
            else:
                lead += f"Checkpoint {i} did not clear its bar, so the test continues. "
    info = look(None, stats.norm.isf(INFORMATIONAL_ALPHA / 2))
    if info is None:
        return none(decision, f"{lead}Not enough usable days per arm yet for an interval "
                    f"({len(observed)} usable of {elapsed}).", last_reached)
    return ExperimentAnalysis(
        days_observed=len(observed), effect_pct=info.effect_pct, ci_low_pct=info.lo_pct, ci_high_pct=info.hi_pct,
        checkpoint_reached=last_reached, decision=decision,  # type: ignore[arg-type]
        note=f"{lead}Informational only, do not act on it: {describe(info)}{corr_note}{third_arm_note(None)}")


# ---------------------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------------------


def days_per_arm(effect_pct: float, resid_cv: float, rho: float, alpha: float, power_: float,
                 n_looks: int = _DEFAULT_LOOKS_FOR_POWER) -> int:
    """Two-sample normal approximation: n = 2 (z_{1-a/2} + z_power)^2 (CV / effect)^2, inflated
    by (1+rho)/(1-rho) for lag-1 autocorrelation and by the group-sequential design's
    inflation for ``n_looks`` O'Brien-Fleming looks."""
    from climate.experiments.switchback import sequential_inflation

    e = abs(effect_pct) / 100.0
    z = stats.norm.isf(alpha / 2.0) + stats.norm.ppf(power_)
    n = 2.0 * z * z * (resid_cv / e) ** 2
    n *= (1.0 + rho) / (1.0 - rho)
    n *= sequential_inflation(alpha, power_, n_looks)
    return max(2, math.ceil(n))


def power(session: Session, effect_pct: float, alpha: float = 0.10, power_: float = 0.8) -> PowerOut:
    """Days per arm needed from the active baselines' residual CV (two-sample normal approx,
    inflated for lag-1 autocorrelation). Without baselines: resid_cv None, days None."""
    if not math.isfinite(effect_pct) or abs(effect_pct) < 0.1:
        raise ValueError("effect_pct must be at least 0.1% in size")
    if not 0 < alpha < 0.5:
        raise ValueError("alpha must be between 0 and 0.5")
    if not 0.5 <= power_ < 1:
        raise ValueError("power must be between 0.5 and 1")
    fits = baseline_mod.active_fits(session)
    cv, rho, mode = pooled_cv(fits, unit_weights(session))
    if cv is None:
        return PowerOut(effect_pct=effect_pct, alpha=alpha, power=power_, resid_cv=None, days_per_arm=None,
                        total_days=None,
                        note="No active weather baselines yet, so the day-to-day noise is unknown. Baselines "
                             "need about three weeks of data; size the test once they pass their checks.")
    n = days_per_arm(effect_pct, cv, rho, alpha, power_)
    n10 = days_per_arm(10.0, cv, rho, alpha, power_)
    n5 = days_per_arm(5.0, cv, rho, alpha, power_)
    if n <= 49:
        verdict = f"About {n} test days per arm ({2 * n} in all): a real test can settle it within a season."
    elif n <= 120:
        verdict = (f"About {n} days per arm ({2 * n} in all): possible, but it takes most of a season; "
                   "consider a bigger change or the simulator first.")
    else:
        verdict = (f"About {n} days per arm: too small to confirm with real days in one season; judge it "
                   "mainly in the simulator.")
    note = (f"{verdict} Day-to-day noise of {mode}ing house runtime after weather: {cv * 100:.0f}% "
            f"(lag-1 correlation {rho:.2f}). For scale, a 10% change needs about {n10} days per arm and a 5% "
            f"change about {n5}, in line with the blueprint's expectation (10-15% within weeks, 5% takes "
            f"months). Assumes {_DEFAULT_LOOKS_FOR_POWER} pre-planned checkpoints at two-sided alpha {alpha:g} "
            f"(a false win for the treatment at most {alpha / 2:g}) and {power_:.0%} power.")
    return PowerOut(effect_pct=effect_pct, alpha=alpha, power=power_, resid_cv=round(cv, 4), days_per_arm=n,
                    total_days=2 * n, note=note)

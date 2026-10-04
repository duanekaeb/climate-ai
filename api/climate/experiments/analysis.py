"""Experiment analysis and power, on weather-normalized daily total-house runtime.

The metric is the daily house residual: actual stage-1 runtime summed over the units (weighted
by ``units.power_weight``) minus what each unit's weather baseline expected for that day's mode
over the same 5-minute slots (``expected_covered_seconds``: a day with 95% of its slots is
compared with 95% of the full-day expectation, never the whole of it). Weather is thereby
removed day by day, and the switchback's randomization handles everything else.
The effect is reported as the difference in mean residual (treatment arm minus the first,
control arm) as a percentage of mean expected runtime: negative means the treatment used LESS
runtime than the control under the same weather.

Each pre-planned checkpoint is judged once, by the nightly run after its last day has left the
late-data refill window, and its verdict is stored on the experiment (``freeze_checkpoints``);
``analyze`` reports stored verdicts and never recomputes them.
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
DEFAULT_BLOCK_DAYS = 2  # the proposal form's default
MAX_BLOCK_DAYS = 7  # ProposeExperimentBody.block_days


# ---------------------------------------------------------------------------------------
# helpers shared with climate.models.backtest
# ---------------------------------------------------------------------------------------


def unit_weights(session: Session) -> dict[str, float]:
    return {
        u.key: float(u.power_weight if u.power_weight is not None else 1.0)
        for u in session.scalars(select(Unit).order_by(Unit.sort)).all()
    }


def pooled_cv(
    fits: dict[tuple[str, str], Any], weights: dict[str, float], mode: str | None = None
) -> tuple[float | None, float, str | None]:
    """(house residual CV, lag-1 autocorrelation, mode) from the active baselines.

    The house CV is the expected-runtime-weighted average of the units' CV(RMSE), i.e. the
    house total's CV if the units' daily errors move together (they share the weather and the
    family's schedule), which is the conservative choice. When ``mode`` is None the mode with
    the most expected runtime is used. Returns (None, 0, None) without usable fits."""
    best: tuple[float | None, float, str | None] = (None, 0.0, None)
    best_mean = -1.0
    for m in ("cool", "heat") if mode is None else (mode,):
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
    """Weighted house actual / expected stage-1 seconds per local day in [start, end], both
    over the slots each unit has data for."""
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


def _house_day(
    d: date,
    rows: dict[str, Any],
    units: list[str],
    weights: dict[str, float],
    fits: dict[tuple[str, str], Any],
    tz: str,
) -> HouseDay:
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
    no_weather: list[str] = []
    for u in units:
        fit = fits.get((u, mode))
        if fit is None:
            no_fit.append(u)
            continue
        e = float(baseline_mod.expected_covered_seconds(fit, rows[u]))  # same slots as the actual
        if not math.isfinite(e):  # no outdoor data that day: the baseline can't say anything
            no_weather.append(u)
            continue
        expected += weights[u] * e
    notes: list[str] = []
    if no_weather:
        notes.append(f"no outdoor temperature data for {', '.join(no_weather)}")
    if thin:
        notes.append(f"under 90% of the day's data for {', '.join(thin)}")
    if mixed:
        notes.append("mixed heating and cooling day")
    if no_fit:
        notes.append(f"no active {mode} baseline for {', '.join(no_fit)}")
    included = not notes
    return HouseDay(d, actual, None if (no_fit or no_weather) else expected, included, "; ".join(notes) or None)


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
    house both heated and cooled that day. Then freezes every checkpoint whose days are all
    past the refill window (``freeze_checkpoints``)."""
    tz = _tz(session)
    today = local_date(now, tz)
    recent = today - timedelta(days=RECENT_REFILL_DAYS)
    rows = session.scalars(
        select(ExperimentDay)
        .join(Experiment, Experiment.id == ExperimentDay.experiment_id)
        .where(
            Experiment.status.in_(("approved", "running", "completed", "stopped")),
            ExperimentDay.day < today,
            or_(
                ExperimentDay.day >= recent,
                and_(ExperimentDay.actual_s.is_(None), ExperimentDay.note.is_(None)),
            ),
        )
    ).all()
    if rows:
        hd = house_days(session, min(r.day for r in rows), max(r.day for r in rows), tz)
        for r in rows:
            h = hd[r.day]
            r.actual_s = h.actual_s
            r.expected_s = h.expected_s
            r.residual_s = (
                (h.actual_s - h.expected_s) if h.actual_s is not None and h.expected_s is not None else None
            )
            r.included = bool(h.included and r.residual_s is not None)
            r.note = h.note
        session.flush()
    freeze_checkpoints(session, now)


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


def _welch(
    res_a: list[float], res_b: list[float], expected: list[float], z_crit: float, deff: float
) -> _Look | None:
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


FINAL_DECISIONS = ("stop_win", "stop_futile", "inconclusive")


@dataclass
class _Context:
    """Everything a look at one experiment needs: arms, checkpoints, the usable days and the
    within-block correlation correction from the active baselines."""

    start: date
    today: date
    arms: list[str]
    labels: dict[str, str]
    checkpoints: list[Checkpoint]
    observed: list[ExperimentDay]  # finished, included days with a residual
    elapsed: int  # finished days
    rho: float
    deff: float

    @property
    def corr_note(self) -> str:
        if self.deff <= 1.0:
            return ""
        return f" Intervals are widened for day-to-day correlation within blocks (lag-1 ≈ {self.rho:.2f})."

    def _pair(self, arm: str, cutoff: date | None, z_crit: float) -> _Look | None:
        sel = [d for d in self.observed if cutoff is None or d.day < cutoff]
        a = self.arms[0]
        res_a = [float(d.residual_s) for d in sel if d.arm == a]  # type: ignore[arg-type]
        res_b = [float(d.residual_s) for d in sel if d.arm == arm]  # type: ignore[arg-type]
        expected = [float(d.expected_s) for d in sel if d.arm in (a, arm) and d.expected_s is not None]
        return _welch(res_a, res_b, expected, z_crit, self.deff)

    def look(self, cutoff: date | None, z_crit: float) -> _Look | None:
        """Treatment (second arm) vs control on the days before ``cutoff`` (all when None)."""
        return self._pair(self.arms[1], cutoff, z_crit)

    def usable(self, cutoff: date | None) -> int:
        return sum(1 for d in self.observed if cutoff is None or d.day < cutoff)

    def describe(self, lk: _Look) -> str:
        a, b = self.labels[self.arms[0]], self.labels[self.arms[1]]
        direction = "less" if lk.effect_pct < 0 else "more"
        return (
            f"'{b}' used {abs(lk.effect_pct):.1f}% {direction} weather-normalized runtime "
            f"than '{a}' ({lk.n_b} vs {lk.n_a} days; {lk.level * 100:.1f}% interval "
            f"{_fmt_pct(lk.lo_pct)} to {_fmt_pct(lk.hi_pct)})."
        )

    def third_arm_note(self, cutoff: date | None) -> str:
        if len(self.arms) < 3:
            return ""
        lk = self._pair(self.arms[2], cutoff, stats.norm.isf(INFORMATIONAL_ALPHA / 2))
        if lk is None:
            return ""
        return (
            f" Secondary arm '{self.labels[self.arms[2]]}' vs '{self.labels[self.arms[0]]}': "
            f"{_fmt_pct(lk.effect_pct)} (90% {_fmt_pct(lk.lo_pct)} to {_fmt_pct(lk.hi_pct)}; not part of the "
            "pre-planned decision, treat it as a lead)."
        )

    def last_day(self, cp: Checkpoint) -> date:
        return self.start + timedelta(days=cp.day - 1)

    def freeze_day(self, cp: Checkpoint) -> date:
        """The local day whose nightly run fixes this checkpoint's verdict: the first night on
        which its last day is older than the refill window."""
        return self.last_day(cp) + timedelta(days=RECENT_REFILL_DAYS + 1)


def _context(session: Session, experiment: Experiment, today: date) -> _Context:
    design = dict(experiment.design or {})
    arms = [a["key"] for a in experiment.arms]
    days = session.scalars(
        select(ExperimentDay).where(ExperimentDay.experiment_id == experiment.id).order_by(ExperimentDay.day)
    ).all()
    fits = baseline_mod.active_fits(session)
    _, rho, _ = pooled_cv(fits, unit_weights(session))
    return _Context(
        start=experiment.start_date or today,
        today=today,
        arms=arms,
        labels={a["key"]: a.get("label") or a["key"] for a in experiment.arms},
        checkpoints=[Checkpoint.model_validate(c) for c in design.get("checkpoints", [])],
        observed=[d for d in days if d.included and d.residual_s is not None and d.day < today],
        elapsed=sum(1 for d in days if d.day < today),
        rho=rho,
        deff=_block_design_effect(rho, int(design.get("block_days", 1))),
    )


def _verdict(ctx: _Context, i: int, cp: Checkpoint, now: datetime) -> dict[str, Any]:
    """The pre-planned decision at checkpoint ``i``, from exactly the days before it: 'stop_win'
    when the interval at the checkpoint's alpha excludes 0 in the treatment's favour; at the
    last look otherwise 'stop_futile' (no better) or 'inconclusive' (leaned better but did not
    clear the bar); 'continue' at an interim look."""
    n_cp = len(ctx.checkpoints)
    cutoff = ctx.start + timedelta(days=cp.day)
    lk = ctx.look(cutoff, cp.z_crit)
    at = f"Checkpoint {i} of {n_cp} (day {cp.day})"
    final = i == n_cp
    if lk is not None and lk.hi_pct < 0:
        decision = "stop_win"
        note = (f"{at}: {ctx.describe(lk)} The whole interval is below zero, which clears this checkpoint's "
                "pre-planned bar: the treatment wins; stop and adopt it.")
    elif lk is None:
        decision = "inconclusive" if final else "continue"
        note = (f"{at}: too few usable days per arm to judge ({ctx.usable(cutoff)} usable of {cp.day})."
                + ("" if final else " The test continues."))
    elif final:
        if lk.effect_pct >= 0 or lk.lo_pct > 0:
            decision = "stop_futile"
            verdict = "The treatment did no better than the control, so it is not worth adopting."
        else:
            decision = "inconclusive"
            verdict = ("It leaned toward the treatment but the interval still includes zero; a real effect "
                       "this small needs months of days, so judge it in the simulator instead.")
        note = f"{at}, the final look: {ctx.describe(lk)} {verdict}"
    else:
        decision = "continue"
        worse = (" The treatment used significantly MORE runtime; consider stopping it."
                 if lk.lo_pct > 0 else "")
        note = (f"{at}: {ctx.describe(lk)} The interval includes zero or favours the control, so it does not "
                f"clear this checkpoint's bar; the test continues.{worse}")
    return {
        "checkpoint": i,
        "day": cp.day,
        "decision": decision,
        "effect_pct": lk.effect_pct if lk else None,
        "ci_low_pct": lk.lo_pct if lk else None,
        "ci_high_pct": lk.hi_pct if lk else None,
        "level": lk.level if lk else None,
        "n_a": lk.n_a if lk else None,
        "n_b": lk.n_b if lk else None,
        "usable_days": ctx.usable(cutoff),
        "lag1": round(ctx.rho, 4),
        "design_effect": round(ctx.deff, 4),
        "note": note + ctx.corr_note + ctx.third_arm_note(cutoff),
        "at": now.isoformat(),
    }


def freeze_checkpoints(session: Session, now: datetime) -> int:
    """Evaluate and STORE each reached checkpoint once, in order, as soon as all of its days are
    older than the nightly refill window (``RECENT_REFILL_DAYS``: until then late runtime reports
    can still change them). The verdict (effect, interval, decision, at, ...) goes to
    ``experiment.result['checkpoints'][str(k)]`` (k from 1) and is never recomputed, so a
    decision cannot drift as baselines are refitted or late data arrives. Stops at a final
    verdict. Returns the number of verdicts stored."""
    tz = _tz(session)
    today = local_date(now, tz)
    settled = today - timedelta(days=RECENT_REFILL_DAYS)  # days before this are never refilled again
    stored_n = 0
    for exp in session.scalars(
        select(Experiment)
        .where(Experiment.status.in_(("approved", "running", "completed", "stopped")),
               Experiment.start_date.is_not(None))
        .order_by(Experiment.id)
    ).all():
        result = dict(exp.result or {})
        stored = {str(k): v for k, v in (result.get("checkpoints") or {}).items()}
        ctx: _Context | None = None
        log = list(result.get("log", []))
        planned = [Checkpoint.model_validate(c) for c in (exp.design or {}).get("checkpoints", [])]
        for i, cp in enumerate(planned):
            k = str(i + 1)
            if k in stored:
                if stored[k].get("decision") in FINAL_DECISIONS:
                    break
                continue
            last = exp.start_date + timedelta(days=cp.day - 1)  # type: ignore[operator]
            if exp.end_date is None or last > exp.end_date or last >= settled:
                break  # never reached (stopped early) or its days may still change
            ctx = ctx or _context(session, exp, today)
            v = _verdict(ctx, i + 1, cp, now)
            stored[k] = v
            log.append(
                {"at": now.isoformat(), "event": "checkpoint", "checkpoint": i + 1, "decision": v["decision"]}
            )
            stored_n += 1
            if v["decision"] in FINAL_DECISIONS:
                break
        if ctx is not None:
            result["checkpoints"] = stored
            result["log"] = log
            exp.result = result
    if stored_n:
        session.flush()
    return stored_n


def _from_verdict(v: dict[str, Any], days_observed: int) -> ExperimentAnalysis:
    fixed = str(v.get("at") or "")[:10]
    return ExperimentAnalysis(
        days_observed=days_observed,
        effect_pct=v.get("effect_pct"),
        ci_low_pct=v.get("ci_low_pct"),
        ci_high_pct=v.get("ci_high_pct"),
        checkpoint_reached=int(v["checkpoint"]),
        decision=v["decision"],
        note=f"{v.get('note', '')} (Verdict fixed {fixed}, from the days up to the checkpoint.)".strip(),
    )


def analyze(session: Session, experiment: Experiment) -> ExperimentAnalysis:
    """Effect = mean residual(arm B) - mean residual(arm A), as % of mean expected runtime.

    Decisions are made only at the pre-planned checkpoints, and each is evaluated once, by the
    nightly ``freeze_checkpoints``, after its days are past the late-data window; this returns
    the stored verdict of the first final one (stop_win, or the last look's stop_futile /
    inconclusive) and never recomputes it. Otherwise the decision is 'continue' ('inconclusive'
    once the owner stopped it) and the effect shown is a 90% interval on all usable days so far,
    labelled informational, alongside the stored interim verdicts and any reached checkpoint
    still waiting to be fixed."""
    tz = _tz(session)
    today = local_date(utcnow(), tz)

    def none(decision: str, note: str, observed: int = 0, reached: int | None = None) -> ExperimentAnalysis:
        return ExperimentAnalysis(
            days_observed=observed,
            effect_pct=None,
            ci_low_pct=None,
            ci_high_pct=None,
            checkpoint_reached=reached,
            decision=decision,  # type: ignore[arg-type]
            note=note,
        )

    if experiment.status in ("proposed", "rejected"):
        return none(
            "not_started",
            "Not approved yet." if experiment.status == "proposed" else "Rejected; it never ran.",
        )
    start = experiment.start_date
    if start is None or today <= start:
        if experiment.status == "stopped":
            return none("inconclusive", "Stopped before its first day; no data.")
        when = f" It starts {start.isoformat()}." if start else ""
        return none("not_started", f"No finished days yet.{when}")

    ctx = _context(session, experiment, today)
    design = dict(experiment.design or {})
    stored = {str(k): v for k, v in ((experiment.result or {}).get("checkpoints") or {}).items()}
    n_obs = len(ctx.observed)
    reached = [(i, cp) for i, cp in enumerate(ctx.checkpoints, start=1) if cp.day <= ctx.elapsed]
    frozen: list[dict[str, Any]] = []
    pending: tuple[int, Checkpoint] | None = None
    for i, cp in reached:
        v = stored.get(str(i))
        if v is None:
            pending = (i, cp)
            break
        if v.get("decision") in FINAL_DECISIONS:
            return _from_verdict(v, n_obs)
        frozen.append(v)

    last_reached = reached[-1][0] if reached else None
    n_cp = len(ctx.checkpoints)
    if experiment.status == "stopped":
        decision = "inconclusive"
        lead = "Stopped by the owner before a final checkpoint verdict, so no pre-planned verdict exists. "
    else:
        decision = "continue"
        nxt = next((cp for cp in ctx.checkpoints if cp.day > ctx.elapsed), None)
        lead = (
            f"Day {ctx.elapsed} of {design.get('n_days', '?')}; next checkpoint on day {nxt.day}. "
            if nxt
            else f"Day {ctx.elapsed}. "
        )
    if frozen:
        v = frozen[-1]
        k = int(v["checkpoint"])
        if v.get("effect_pct") is None:
            lead += f"Checkpoint {k} had too few usable days to judge, so the test continues. "
        else:
            lead += (
                f"Checkpoint {k} did not clear its bar ({_fmt_pct(v['effect_pct'])}, "
                f"{float(v['level']) * 100:.1f}% interval {_fmt_pct(v['ci_low_pct'])} to "
                f"{_fmt_pct(v['ci_high_pct'])}; fixed {str(v.get('at') or '')[:10]}), so the test continues. "
            )
            if v["ci_low_pct"] > 0:
                lead += ("At that checkpoint the treatment used significantly MORE runtime; "
                         "consider stopping it. ")
    if pending is not None:
        i, cp = pending
        which = "The final checkpoint" if i == n_cp else f"Checkpoint {i} of {n_cp}"
        lead += (
            f"{which} (day {cp.day}) is reached; its verdict is fixed in the nightly run on "
            f"{ctx.freeze_day(cp).isoformat()}, once its last day is past the "
            f"{RECENT_REFILL_DAYS}-day window in which late runtime reports can still change it. "
        )
    info = ctx.look(None, stats.norm.isf(INFORMATIONAL_ALPHA / 2))
    if info is None:
        return none(
            decision,
            f"{lead}Not enough usable days per arm yet for an interval ({n_obs} usable of {ctx.elapsed}).",
            n_obs,
            last_reached,
        )
    return ExperimentAnalysis(
        days_observed=n_obs,
        effect_pct=info.effect_pct,
        ci_low_pct=info.lo_pct,
        ci_high_pct=info.hi_pct,
        checkpoint_reached=last_reached,
        decision=decision,  # type: ignore[arg-type]
        note=f"{lead}Informational only, not a decision, do not act on it: {ctx.describe(info)}"
        f"{ctx.corr_note}{ctx.third_arm_note(None)}",
    )


# ---------------------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------------------


def days_per_arm(
    effect_pct: float,
    resid_cv: float,
    rho: float,
    alpha: float,
    power_: float,
    n_looks: int = _DEFAULT_LOOKS_FOR_POWER,
    block_days: int = DEFAULT_BLOCK_DAYS,
) -> int:
    """Two-sample normal approximation: n = 2 (z_{1-a/2} + z_power)^2 (CV / effect)^2, inflated
    by the SAME within-block design effect ``analyze`` widens its intervals by
    (``_block_design_effect(rho, block_days)``: days are randomized in blocks, so only the
    correlation inside a block costs information) and by the group-sequential design's
    inflation for ``n_looks`` O'Brien-Fleming looks."""
    from climate.experiments.switchback import sequential_inflation

    e = abs(effect_pct) / 100.0
    z = stats.norm.isf(alpha / 2.0) + stats.norm.ppf(power_)
    n = 2.0 * z * z * (resid_cv / e) ** 2
    n *= _block_design_effect(rho, block_days)
    n *= sequential_inflation(alpha, power_, n_looks)
    return max(2, math.ceil(n))


def power(
    session: Session, effect_pct: float, alpha: float = 0.10, power_: float = 0.8,
    block_days: int = DEFAULT_BLOCK_DAYS,
) -> PowerOut:
    """Days per arm needed from the active baselines' residual CV (two-sample normal approx,
    inflated by the analysis' within-block design effect for ``block_days``-day blocks and by
    the checkpoints). Without baselines: resid_cv None, days None."""
    if not math.isfinite(effect_pct) or abs(effect_pct) < 0.1:
        raise ValueError("effect_pct must be at least 0.1% in size")
    if not 0 < alpha < 0.5:
        raise ValueError("alpha must be between 0 and 0.5")
    if not 0.5 <= power_ < 1:
        raise ValueError("power must be between 0.5 and 1")
    if not 1 <= int(block_days) <= MAX_BLOCK_DAYS:
        raise ValueError(f"block_days must be between 1 and {MAX_BLOCK_DAYS}")
    fits = baseline_mod.active_fits(session)
    cv, rho, mode = pooled_cv(fits, unit_weights(session))
    if cv is None:
        return PowerOut(
            effect_pct=effect_pct,
            alpha=alpha,
            power=power_,
            resid_cv=None,
            days_per_arm=None,
            total_days=None,
            note="No active weather baselines yet, so the day-to-day noise is unknown. Baselines "
            "need about three weeks of data; size the test once they pass their checks.",
        )
    n = days_per_arm(effect_pct, cv, rho, alpha, power_, block_days=block_days)
    n10 = days_per_arm(10.0, cv, rho, alpha, power_, block_days=block_days)
    n5 = days_per_arm(5.0, cv, rho, alpha, power_, block_days=block_days)
    deff = _block_design_effect(rho, block_days)
    if n <= 49:
        verdict = f"About {n} test days per arm ({2 * n} in all): a real test can settle it within a season."
    elif n <= 120:
        verdict = (
            f"About {n} days per arm ({2 * n} in all): possible, but it takes most of a season; "
            "consider a bigger change or the simulator first."
        )
    else:
        verdict = (
            f"About {n} days per arm: too small to confirm with real days in one season; judge it "
            "mainly in the simulator."
        )
    note = (
        f"{verdict} Day-to-day noise of {mode}ing house runtime after weather: {cv * 100:.0f}% "
        f"(lag-1 correlation {rho:.2f}). For scale, a 10% change needs about {n10} days per arm and a 5% "
        f"change about {n5}, in line with the blueprint's expectation (10-15% within weeks, 5% takes "
        f"months). Assumes {block_days}-day blocks (correlation within a block costs a factor "
        f"{deff:.2f} in days, the same widening the analysis applies), {_DEFAULT_LOOKS_FOR_POWER} "
        f"pre-planned checkpoints at two-sided alpha {alpha:g} (a false win for the treatment at most "
        f"{alpha / 2:g}) and {power_:.0%} power."
    )
    return PowerOut(
        effect_pct=effect_pct,
        alpha=alpha,
        power=power_,
        resid_cv=round(cv, 4),
        days_per_arm=n,
        total_days=2 * n,
        note=note,
    )

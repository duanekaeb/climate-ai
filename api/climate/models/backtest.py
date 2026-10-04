"""Backtests and simulations for the change gates and the agent's tools.

Both replay the house's own recorded inputs (outdoor temperature, sun, which floors were
occupied) under two policies and compare them through a house model:

- ``model='rc'`` when an active RC fit exists for the days' mode: the 3-zone model in
  ``thermal_rc`` with an ideal-thermostat emulation (each unit runs just enough each 15
  minutes to hold its setpoint, up to full capacity), so floor coupling and equipment limits
  are part of the answer.
- ``model='rule_of_thumb'`` otherwise: each unit's weather baseline, treating a setpoint
  change of Δ°F as a balance-point shift of Δ°F (runtime change ≈ slope × degree-hours). It
  cannot see floor coupling or capacity limits.

Setpoints come from a compact emulation of the linked-floors rules in ``control.policy.plan``
(an approximation, documented in ``policy_setpoints``): comfort bands by period (night when a
unit's sleep room is inside its sleep window, else day; away for a unit with no occupied room
by day), linked floors (main floor empty by day while anyone is upstairs: main cool =
min(main day cool, upstairs cool - linked_offset_f), main heat >= upstairs heat -
linked_heat_gap_f), pre-cool on hot sunny afternoons, hard-limit clamping. Not replayed:
learned room offsets and priority rooms, and the whole-house-empty setback and recovery lead
(the history holds no phone presence, and uncertain means occupied), so ``setback_gap_f`` and
``recovery_lead_min`` show no effect here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from climate.analytics import baseline as baseline_mod
from climate.analytics.daily import heat_metrics
from climate.api.schemas import BacktestOut, SimPoint, SimulateOut
from climate.control.guardrails import round_setpoint
from climate.control.policy import PolicyParams
from climate.experiments.analysis import pooled_cv
from climate.house import ROOM_BY_KEY
from climate.models import thermal_rc as rc
from climate.store.app_settings import (
    DEFAULT_COMFORT,
    ControlSettings,
    LocationSettings,
    OccupancySettings,
    get_setting,
)
from climate.store.orm import PolicyVersion, Unit
from climate.timeutil import day_bounds_utc, in_window, local_date, utcnow

ZONES = rc.ZONES
OCCUPIED_TOL_F = 1.0  # blueprint §3: occupied rooms ≤ 1°F outside their band
ASLEEP_TOL_F = 0.5  # asleep: ≤ 0.5°F
MIN_INPUT_FRACTION = 0.9
WARMUP_SLOTS = 6 * rc.SLOTS_PER_HOUR  # replay spin-up before each day, not counted
OPEN_METEO_NOTE = " Weather data by Open-Meteo.com."
# SimulateOut's day field: the contract currently spells it "Date"; accept "date" too.
_SIM_DAY_FIELD = "date" if "date" in SimulateOut.model_fields else "Date"


# ---------------------------------------------------------------------------------------
# policy and environment
# ---------------------------------------------------------------------------------------


@dataclass
class Env:
    control: ControlSettings
    occupancy: OccupancySettings
    tz: str
    weights: np.ndarray  # power_weight per zone, ZONES order


def load_env(session: Session) -> Env:
    w = {
        u.key: float(u.power_weight if u.power_weight is not None else 1.0)
        for u in session.scalars(select(Unit)).all()
    }
    return Env(
        control=get_setting(session, "control", ControlSettings),
        occupancy=get_setting(session, "occupancy", OccupancySettings),
        tz=get_setting(session, "location", LocationSettings).tz,
        weights=np.array([w.get(z, 1.0) for z in ZONES]),
    )


def active_policy(session: Session) -> PolicyParams:
    row = session.scalars(
        select(PolicyVersion)
        .where(PolicyVersion.status == "active")
        .order_by(PolicyVersion.created_at.desc(), PolicyVersion.id.desc())
    ).first()
    return PolicyParams.model_validate(row.params) if row is not None else PolicyParams()


def candidate_policy(active: PolicyParams, params: dict[str, Any]) -> PolicyParams:
    """Active params overlaid with a partial PolicyParams. ValueError on unknown keys or values."""
    unknown = sorted(set(params) - set(PolicyParams.model_fields))
    if unknown:
        raise ValueError(f"unknown policy parameter(s): {', '.join(unknown)}")
    try:
        return PolicyParams.model_validate({**active.model_dump(), **params})
    except Exception as exc:  # pydantic.ValidationError, surfaced as a plain ValueError
        raise ValueError(str(exc)) from exc


def _is_night(env: Env, unit: str, lt: datetime) -> bool:
    for room, windows in env.occupancy.sleep_windows.items():
        rd = ROOM_BY_KEY.get(room)
        if rd is None or rd.unit_key != unit:
            continue
        if any(in_window(lt, w.start, w.end, w.days) for w in windows):
            return True
    return False


def policy_setpoints(
    pp: PolicyParams, env: Env, lt: datetime, occ: np.ndarray, hot_sunny: bool
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(heat_f, cool_f, comfort_low, comfort_high) per zone for one local time.

    Approximation of ``control.policy.plan``: occupancy per unit is "any of its sensors saw
    someone in the slot", and unknown counts as occupied. comfort_low/high are the owner's
    band for an occupied (or asleep) unit widened by the allowed excursion, NaN for an empty
    unit; they are what comfort violations are counted against."""
    comfort, lim = env.control.comfort, env.control.limits
    heat, cool = np.empty(3), np.empty(3)
    lo, hi = np.full(3, np.nan), np.full(3, np.nan)
    night = [_is_night(env, u, lt) for u in ZONES]
    occupied = [night[z] or occ[z] != 0 for z in range(3)]  # NaN (unknown) -> occupied
    for z, u in enumerate(ZONES):
        uc = comfort.get(u) or DEFAULT_COMFORT[u]
        band = uc.night if night[z] else (uc.day if occupied[z] else uc.away)
        heat[z], cool[z] = band.heat_f, band.cool_f
        if occupied[z]:
            tol = ASLEEP_TOL_F if night[z] else OCCUPIED_TOL_F
            lo[z], hi[z] = band.heat_f - tol, band.cool_f + tol
    m, up = ZONES.index("main"), ZONES.index("up")
    linked = pp.linked_floors_enabled and not night[m] and not occupied[m] and occupied[up]
    if linked:
        main_day = (comfort.get("main") or DEFAULT_COMFORT["main"]).day
        cool[m] = min(main_day.cool_f, cool[up] - pp.linked_offset_f)
        heat[m] = max(heat[m], heat[up] - pp.linked_heat_gap_f)
    if pp.precool_enabled and hot_sunny and lt.hour >= pp.precool_start_hour:
        for z in range(3):
            if not night[z] and (occupied[z] or (z == m and linked)):
                cool[z] -= pp.precool_degrees_f
    cool = np.clip(cool, lim.min_cool_f, lim.max_cool_f)
    heat = np.clip(heat, lim.min_heat_f, lim.max_heat_f)
    heat = np.minimum(heat, cool - lim.min_deadband_f)
    return (np.array([round_setpoint(x) for x in heat]), np.array([round_setpoint(x) for x in cool]), lo, hi)


def _hot_sunny_days(pp: PolicyParams, s: rc.Series) -> dict[date, bool]:
    """Per local day: max outdoor >= the pre-cool threshold and a sunny afternoon (mean cloud
    cover 12:00-18:00 under 40%, or mean shortwave over 0.4 kW/m² when cloud is unknown)."""
    out: dict[date, bool] = {}
    lt = s.local
    for d, (i0, i1) in s.day_slices().items():
        t = s.t_out[i0:i1]
        hot = bool(np.isfinite(t).any() and np.nanmax(t) >= pp.precool_min_forecast_high_f)
        aft = [i for i in range(i0, i1) if 12 <= lt[i].hour < 18]
        cloud = s.cloud[aft] if aft else np.array([])
        sun = s.sun[aft] if aft else np.array([])
        if np.isfinite(cloud).any():
            sunny = float(np.nanmean(cloud)) < 40.0
        elif np.isfinite(sun).any():
            sunny = float(np.nanmean(sun)) > 0.4
        else:
            sunny = False
        out[d] = hot and sunny
    return out


@dataclass
class Schedule:
    heat: np.ndarray  # (n, 3)
    cool: np.ndarray
    lo: np.ndarray
    hi: np.ndarray


def schedule(pp: PolicyParams, env: Env, s: rc.Series) -> Schedule:
    hs = _hot_sunny_days(pp, s)
    lt = s.local
    n = s.n
    out = Schedule(np.empty((n, 3)), np.empty((n, 3)), np.empty((n, 3)), np.empty((n, 3)))
    for i in range(n):
        h, c, lo, hi = policy_setpoints(pp, env, lt[i], s.occ[i], hs.get(lt[i].date(), False))
        out.heat[i], out.cool[i], out.lo[i], out.hi[i] = h, c, lo, hi
    return out


# ---------------------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------------------


@dataclass
class _Day:
    day: date
    i0: int
    i1: int
    mode: str
    j0: int = -1  # where the replay starts: i0 minus the warm-up when the data allows it


def _initial_state(s: rc.Series, d: _Day) -> tuple[int, np.ndarray] | None:
    """Start the replay WARMUP_SLOTS before the day from the recorded temperatures, so by
    midnight the model house is already running the replayed policy and the day itself is not
    charged for converging from whatever the real thermostats were doing. Falls back to the
    day's own first recorded temperatures when the hours before are missing."""
    j = d.i0 - WARMUP_SLOTS
    if j >= 0 and np.isfinite(s.t_out[j : d.i0]).mean() >= 0.5:
        T0 = _start_temp(s, j)
        if T0 is not None:
            return j, T0
    T0 = _start_temp(s, d.i0)
    return None if T0 is None else (d.i0, T0)


def _usable_days(s: rc.Series) -> list[_Day]:
    modes = rc.day_modes(s)
    out = []
    for d, (i0, i1) in s.day_slices().items():
        if d not in modes or i1 - i0 < 90:
            continue
        if np.isfinite(s.t_out[i0:i1]).mean() < MIN_INPUT_FRACTION:
            continue
        out.append(_Day(d, i0, i1, modes[d]))
    return out


def _start_temp(s: rc.Series, i0: int) -> np.ndarray | None:
    for i in range(i0, min(i0 + rc.SLOTS_PER_HOUR, s.n)):
        if np.isfinite(s.temp[i]).all():
            return s.temp[i].copy()
    return None


def _batch(s: rc.Series, days: list[_Day], sched: Schedule, mode: str) -> tuple[np.ndarray, ...]:
    """Pad a set of days (each from its warm-up start j0 to its end) into (n_max, B, ...)
    arrays for one batched emulation; ``count`` marks the slots that belong to the day."""
    n_max = max(d.i1 - d.j0 for d in days)
    B = len(days)
    t_out = np.full((n_max, B), np.nan)
    sun = np.zeros((n_max, B))
    sp = np.full((n_max, B, 3), np.nan)
    lo = np.full((n_max, B, 3), np.nan)
    hi = np.full((n_max, B, 3), np.nan)
    count = np.zeros((n_max, B), dtype=bool)
    for b, d in enumerate(days):
        k = d.i1 - d.j0
        t_out[:k, b] = rc.fill_gaps(s.t_out[d.j0 : d.i1])
        sun[:k, b] = np.nan_to_num(rc.fill_gaps(s.sun[d.j0 : d.i1]))
        sp[:k, b] = (sched.cool if mode == "cool" else sched.heat)[d.j0 : d.i1]
        lo[:k, b], hi[:k, b] = sched.lo[d.j0 : d.i1], sched.hi[d.j0 : d.i1]
        count[d.i0 - d.j0 : k, b] = True
    return t_out, sun, sp, lo, hi, count


def _replay(
    model: rc.RCModel,
    T0: np.ndarray,
    t_out: np.ndarray,
    sun: np.ndarray,
    sp: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    count: np.ndarray,
    weights: np.ndarray,
) -> tuple[float, float]:
    """(weighted runtime seconds, comfort-violation minutes) over the counted slots of a batch."""
    T, on = rc.emulate(model, T0, t_out, sun, sp)
    runtime = float((on * count[..., None] * rc.SLOT_S).sum(axis=(0, 1)) @ weights)
    after = T[1:]
    with np.errstate(invalid="ignore"):
        bad = (after > hi) if model.mode == "cool" else (after < lo)
    bad &= count[..., None]
    return runtime, float(bad.sum()) * rc.SLOT_S / 60.0


def _rc_backtest(
    s: rc.Series,
    env: Env,
    days: list[_Day],
    fits: dict[str, tuple[Any, rc.RCModel]],
    cur: Schedule,
    cand: Schedule,
    skipped: int,
) -> BacktestOut:
    totals = {"cur": 0.0, "cand": 0.0, "vcur": 0.0, "vcand": 0.0}
    by_mode: dict[str, list[_Day]] = {}
    for d in days:
        by_mode.setdefault(d.mode, []).append(d)
    used = 0
    for mode, ds in by_mode.items():
        model = fits[mode][1]
        ready: list[_Day] = []
        temps: list[np.ndarray] = []
        for d in ds:
            init = _initial_state(s, d)
            if init is not None:
                d.j0 = init[0]
                ready.append(d)
                temps.append(init[1])
        if not ready:
            continue
        T0 = np.array(temps)
        used += len(ready)
        for key, sched in (("cur", cur), ("cand", cand)):
            t_out, sun, sp, lo, hi, count = _batch(s, ready, sched, mode)
            rt, viol = _replay(model, T0, t_out, sun, sp, lo, hi, count, env.weights)
            totals[key] += rt
            totals["v" + key] += viol
    if used == 0 or totals["cur"] <= 0:
        return BacktestOut(
            days=used,
            model="rc",
            current_runtime_min=None,
            candidate_runtime_min=None,
            delta_pct=None,
            ci90_pct=None,
            comfort_violation_min_current=None,
            comfort_violation_min_candidate=None,
            beats_model_uncertainty=False,
            note="The house model found no runtime to compare on these days.",
        )
    delta = (totals["cand"] - totals["cur"]) / totals["cur"]
    delta_pct = 100.0 * delta
    main_mode = max(by_mode, key=lambda m: len(by_mode[m]))
    metrics = dict(fits[main_mode][0].metrics or {})
    err = metrics.get("runtime_err_rmse")
    scatter = metrics.get("runtime_err_scatter", err)
    if err is not None and scatter is not None:
        # Held-out daily runtime error of the model, propagated to the period comparison: the
        # day-to-day scatter averages over the replayed days (conservatively ignoring that both
        # replays share the same days), and the overall error is taken to scale the simulated
        # difference itself (a model that misjudges runtime by 10% misjudges a change by ~10%).
        half = rc.Z90 * 100.0 * math.sqrt(float(scatter) ** 2 / used + (float(err) * delta) ** 2)
        ci: tuple[float, float] | None = (delta_pct - half, delta_pct + half)
        beats = abs(delta_pct) > half
        unc = (
            f"90% interval {ci[0]:+.1f}% to {ci[1]:+.1f}% from the model's held-out runtime error "
            f"({float(err) * 100:.0f}% per day). "
        )
    else:
        ci, beats = None, False
        unc = "This fit has no held-out runtime error, so the result cannot be judged against it. "
    verdict = (
        "Beats the model's own uncertainty: worth a real trial."
        if beats
        else "Does not beat the model's own uncertainty: not worth a real day yet."
    )
    skip = f" {skipped} day(s) skipped (missing data or no active model for their mode)." if skipped else ""
    note = (
        f"Replayed {used} recorded day(s) through the {main_mode}ing house model with the recorded weather "
        f"and occupancy. Current policy {totals['cur'] / 60:.0f} min, "
        f"candidate {totals['cand'] / 60:.0f} min ({delta_pct:+.1f}%). {unc}{verdict} Comfort-violation minutes: {totals['vcur']:.0f} -> "
        f"{totals['vcand']:.0f}.{skip} Approximation: an ideal thermostat holding the linked-floors "
        "setpoints (each day after a 6-hour uncounted warm-up); room offsets and whole-house-empty "
        "setbacks are not replayed."
    )
    if "open-meteo" in s.weather_sources:
        note += OPEN_METEO_NOTE
    return BacktestOut(
        days=used,
        model="rc",
        current_runtime_min=round(totals["cur"] / 60, 1),
        candidate_runtime_min=round(totals["cand"] / 60, 1),
        delta_pct=round(delta_pct, 2),
        ci90_pct=(round(ci[0], 2), round(ci[1], 2)) if ci else None,
        comfort_violation_min_current=round(totals["vcur"], 1),
        comfort_violation_min_candidate=round(totals["vcand"], 1),
        beats_model_uncertainty=beats,
        note=note,
    )


def _setpoint_ref(s: rc.Series, mode: str, z: int, idx: np.ndarray, fallback: np.ndarray) -> float:
    """The setpoint the baseline was fitted under: the recorded mean, else the schedule's."""
    rec = (s.cool_sp if mode == "cool" else s.heat_sp)[idx, z]
    if np.isfinite(rec).any():
        return float(np.nanmean(rec))
    return float(np.mean(fallback))


def _degree_slots(t_out: np.ndarray, bp: float, sp: np.ndarray, ref: float, mode: str) -> np.ndarray:
    """Degree-days per slot with the balance point shifted by the setpoint change."""
    shift = sp - ref
    dd = (t_out - bp - shift) if mode == "cool" else (bp + shift - t_out)
    return np.maximum(0.0, dd) * rc.DT_H / 24.0


def _rule_of_thumb(
    session: Session, s: rc.Series, env: Env, days: list[_Day], cur: Schedule, cand: Schedule
) -> BacktestOut:
    fits = baseline_mod.active_fits(session)
    empty = BacktestOut(
        days=0,
        model="rule_of_thumb",
        current_runtime_min=None,
        candidate_runtime_min=None,
        delta_pct=None,
        ci90_pct=None,
        comfort_violation_min_current=None,
        comfort_violation_min_candidate=None,
        beats_model_uncertainty=False,
        note="",
    )
    if not fits:
        empty.note = (
            "No house model and no weather baselines yet, so nothing can be replayed. Baselines need "
            "about three weeks of runtime history."
        )
        return empty
    ok = [d for d in days if all((u, d.mode) in fits for u in ZONES)]
    if not ok:
        empty.note = "No replayable days: every recorded day lacks data or a baseline for its mode."
        return empty
    cur_s = cand_s = 0.0
    for mode in {d.mode for d in ok}:
        ds = [d for d in ok if d.mode == mode]
        idx = np.concatenate([np.arange(d.i0, d.i1) for d in ds])
        on = s.on_cool if mode == "cool" else s.on_heat
        for z, u in enumerate(ZONES):
            fit = fits[(u, mode)]
            sp_cur = (cur.cool if mode == "cool" else cur.heat)[:, z]
            sp_cand = (cand.cool if mode == "cool" else cand.heat)[:, z]
            ref = _setpoint_ref(s, mode, z, idx, sp_cur[idx])
            for d in ds:
                t_out = rc.fill_gaps(s.t_out[d.i0 : d.i1])
                actual = float(np.nansum(on[d.i0 : d.i1, z])) * rc.SLOT_S
                slope = float(fit.slope_s_per_dd)
                change = slope * float(
                    (
                        _degree_slots(t_out, fit.balance_point_f, sp_cand[d.i0 : d.i1], ref, mode)
                        - _degree_slots(t_out, fit.balance_point_f, sp_cur[d.i0 : d.i1], ref, mode)
                    ).sum()
                )
                cur_s += env.weights[z] * actual
                cand_s += env.weights[z] * max(0.0, actual + change)
    if cur_s <= 0:
        empty.days = len(ok)
        empty.note = "The replayed days have no runtime to compare."
        return empty
    delta_pct = 100.0 * (cand_s - cur_s) / cur_s
    w = {u: float(env.weights[z]) for z, u in enumerate(ZONES)}
    main_mode = max({d.mode for d in ok}, key=lambda m: sum(1 for d in ok if d.mode == m))
    cv, _, _ = pooled_cv(fits, w, main_mode)
    bar = 2.0 * cv * 100.0 if cv is not None else math.inf
    beats = abs(delta_pct) > bar
    note = (
        f"Rule of thumb (no house model has passed its checks yet): {len(ok)} recorded day(s), each unit's "
        f"weather baseline with a setpoint change treated as a balance-point shift. Recorded "
        f"{cur_s / 60:.0f} min; candidate {cand_s / 60:.0f} min ({delta_pct:+.1f}%). "
        + (
            f"The bar is twice the baselines' day-to-day noise ({bar:.0f}%): "
            if cv is not None
            else "No baseline noise estimate: "
        )
        + ("beats it." if beats else "does not beat it, so it needs the house model or a real test.")
        + " It ignores floor coupling and equipment limits, so linked-floors effects on the upstairs are "
        "invisible to it."
    )
    return BacktestOut(
        days=len(ok),
        model="rule_of_thumb",
        current_runtime_min=round(cur_s / 60, 1),
        candidate_runtime_min=round(cand_s / 60, 1),
        delta_pct=round(delta_pct, 2),
        ci90_pct=None,
        comfort_violation_min_current=None,
        comfort_violation_min_candidate=None,
        beats_model_uncertainty=beats,
        note=note,
    )


def backtest(session: Session, params: dict, days: int = 28) -> BacktestOut:
    """Replay the last ``days`` with the current policy and with the candidate params
    (partial PolicyParams over the active ones). With an active RC fit: simulate both and
    compare total runtime and comfort-violation minutes; ci90 from the fit's residual error.
    Without one: model='rule_of_thumb' (runtime sensitivity per °F of setpoint from the
    baselines' slope), beats_model_uncertainty False unless the effect exceeds 2x the
    baseline CV. Raises ValueError for unknown or out-of-range params."""
    env = load_env(session)
    active = active_policy(session)
    cand = candidate_policy(active, dict(params))
    today = local_date(utcnow(), env.tz)
    start, _ = day_bounds_utc(today - timedelta(days=days), env.tz)
    end, _ = day_bounds_utc(today, env.tz)
    s = rc.load_series(session, start - WARMUP_SLOTS * rc.SLOT, end, env.tz)
    usable = _usable_days(s)
    sched_cur, sched_cand = schedule(active, env, s), schedule(cand, env, s)
    fits = rc.active_rc(session)
    rc_days = [d for d in usable if d.mode in fits]
    if rc_days and len(rc_days) * 2 >= len(usable):
        return _rc_backtest(s, env, rc_days, fits, sched_cur, sched_cand, skipped=days - len(rc_days))
    return _rule_of_thumb(session, s, env, usable, sched_cur, sched_cand)


# ---------------------------------------------------------------------------------------
# simulate one day
# ---------------------------------------------------------------------------------------

_RECENT_SQL = text(
    """
    SELECT unit_key, sum(comp_cool1) AS cool, sum(comp_heat1) AS heat1, sum(aux_heat1) AS aux1,
           avg(cool_sp_f) AS csp, avg(heat_sp_f) AS hsp
    FROM runtime_5m WHERE ts >= :s AND ts < :e GROUP BY unit_key
    """
)


def _future_series(session: Session, day: date, tz: str) -> tuple[rc.Series, str]:
    """Inputs for a day without records: the forecast (or observed) weather for the day,
    else the same day last week's weather; occupancy from the same day last week."""
    start, end = day_bounds_utc(day, tz)
    n = int((end - start).total_seconds() // rc.SLOT_S)
    t_out, sun, cloud, sources = rc.weather_inputs(session, start, n)
    prev_start, prev_end = day_bounds_utc(day - timedelta(days=7), tz)
    prev = rc.load_series(session, prev_start, prev_end, tz)

    def fit_len(a: np.ndarray) -> np.ndarray:
        if a.shape[0] >= n:
            return a[:n].copy()
        pad = np.full((n - a.shape[0],) + a.shape[1:], np.nan)
        return np.concatenate([a, pad])

    if np.isfinite(t_out).mean() >= 0.5:
        how = "the weather forecast for the day"
    else:
        t_out, sun, cloud = fit_len(prev.t_out), fit_len(prev.sun), fit_len(prev.cloud)
        sources = prev.weather_sources
        how = "the same day last week's weather (no forecast stored for this day)"
    nan3 = np.full((n, 3), np.nan)
    s = rc.Series(
        start=rc.floor15(start),
        tz=tz,
        temp=nan3.copy(),
        on_cool=nan3.copy(),
        on_heat=nan3.copy(),
        cool_sp=nan3.copy(),
        heat_sp=nan3.copy(),
        hvac_off=np.zeros((n, 3), dtype=bool),
        t_out=t_out,
        sun=sun,
        cloud=cloud,
        occ=fit_len(prev.occ),
        weather_sources=sources,
    )
    return s, how


def _choose_mode(session: Session, s: rc.Series, day: date, recorded: bool, now: datetime) -> str:
    if recorded:
        m = rc.day_modes(s).get(day)
        if m is not None:
            return m
    rows = session.execute(_RECENT_SQL, {"s": now - timedelta(days=3), "e": now}).all()
    heat_is_comp = heat_metrics(session)  # each unit's stage-1 heating metric, as in the analytics
    cool = sum(float(r.cool or 0) for r in rows)
    heat = sum(float((r.heat1 if heat_is_comp.get(r.unit_key, False) else r.aux1) or 0) for r in rows)
    if cool > 0 or heat > 0:
        return "cool" if cool >= heat else "heat"
    t = s.t_out[np.isfinite(s.t_out)]
    return "cool" if t.size and float(t.mean()) >= 65.0 else "heat"


def _sim_out(
    day: date, model: str, units: dict[str, list[SimPoint]], total_min: float, note: str
) -> SimulateOut:
    return SimulateOut.model_validate(
        {_SIM_DAY_FIELD: day, "model": model, "units": units, "total_runtime_min": total_min, "note": note}
    )


def simulate(session: Session, params: dict, day: date | None = None) -> SimulateOut:
    """One local day (default tomorrow) under the candidate policy (active params overlaid
    with ``params``): per-unit 15-minute temperature (at the start of each slot) and stage-1
    runtime. A past day replays its recorded weather and occupancy, starting from the recorded
    temperatures six hours earlier (an uncounted warm-up); a future day uses the forecast (else
    last week's weather) and last week's occupancy, starting each unit at its setpoint."""
    env = load_env(session)
    now = utcnow()
    today = local_date(now, env.tz)
    day = day or today + timedelta(days=1)
    pp = candidate_policy(active_policy(session), dict(params))
    recorded = day < today
    day_start, day_end = day_bounds_utc(day, env.tz)
    if recorded:
        s = rc.load_series(session, day_start - WARMUP_SLOTS * rc.SLOT, day_end, env.tz)
        how = "the recorded weather and occupancy"
    else:
        s, how = _future_series(session, day, env.tz)
    w = max(0, s.index_of(rc.floor15(day_start)))  # first slot of the day itself
    if s.n - w <= 0:
        raise ValueError("no slots in that day")
    mode = _choose_mode(session, s, day, recorded, now)
    sched = schedule(pp, env, s)
    sp = sched.cool if mode == "cool" else sched.heat
    t_out = rc.fill_gaps(s.t_out)
    sun = np.nan_to_num(rc.fill_gaps(s.sun))
    attribution = OPEN_METEO_NOTE if "open-meteo" in s.weather_sources else ""
    if not np.isfinite(t_out).all():
        return _sim_out(
            day,
            "rule_of_thumb",
            {},
            0.0,
            "No outdoor temperature for that day or the week before, so nothing can be simulated.",
        )
    span = range(w, s.n)
    fits = rc.active_rc(session)
    if mode in fits:
        model = fits[mode][1]
        j0, T0 = 0, (_start_temp(s, 0) if recorded else None)
        if T0 is None and recorded:
            j0, T0 = w, _start_temp(s, w)
        start_note = (
            "the recorded temperatures, after a 6-hour warm-up"
            if T0 is not None and j0 < w
            else "its recorded midnight temperatures"
            if T0 is not None
            else "each unit at its setpoint"
        )
        if T0 is None:
            j0, T0 = w, sp[w].copy()
        T, on = rc.emulate(model, T0[None, :], t_out[j0:, None], sun[j0:, None], sp[j0:, None, :])
        units = {
            u: [
                SimPoint(
                    ts=s.ts(i),
                    temp_f=round(float(T[i - j0, 0, z]), 2),
                    runtime_s=round(float(on[i - j0, 0, z] * rc.SLOT_S), 1),
                )
                for i in span
            ]
            for z, u in enumerate(ZONES)
        }
        total = float((on[w - j0 :, 0, :] * rc.SLOT_S).sum(axis=0) @ env.weights) / 60.0
        note = (
            f"House model ({mode}ing), {how}, starting from {start_note}; an ideal thermostat holding the "
            f"policy's setpoints. Treat the trajectory as coarse: day-ahead errors of a couple of °F are "
            f"normal for this kind of model.{attribution}"
        )
        return _sim_out(day, "rc", units, round(total, 1), note)

    fits_b = baseline_mod.active_fits(session)
    have = [u for u in ZONES if (u, mode) in fits_b]
    if not have:
        return _sim_out(
            day,
            "rule_of_thumb",
            {},
            0.0,
            f"No house model and no {mode}ing baselines yet, so this day cannot be simulated.",
        )
    recent = {
        r.unit_key: r for r in session.execute(_RECENT_SQL, {"s": now - timedelta(days=14), "e": now}).all()
    }
    units = {}
    total = 0.0
    for z, u in enumerate(ZONES):
        if u not in have:
            continue
        fit = fits_b[(u, mode)]
        r = recent.get(u)
        rec_sp = (r.csp if mode == "cool" else r.hsp) if r is not None else None
        ref = float(rec_sp) if rec_sp is not None else float(np.mean(sp[w:, z]))
        dd = _degree_slots(t_out[w:], fit.balance_point_f, sp[w:, z], ref, mode)
        run = np.clip(
            float(fit.slope_s_per_dd) * dd + float(fit.intercept_s) * rc.DT_H / 24.0, 0.0, rc.SLOT_S
        )
        units[u] = [
            SimPoint(ts=s.ts(i), temp_f=float(sp[i, z]), runtime_s=round(float(run[i - w]), 1)) for i in span
        ]
        total += float(env.weights[z] * run.sum()) / 60.0
    missing = [u for u in ZONES if u not in have]
    note = (
        f"Rule of thumb ({mode}ing baselines; no house model has passed its checks yet), {how}. Temperatures "
        "shown are the setpoints the policy would hold, not predictions; runtime follows each unit's "
        "degree-hours with the setpoint as a balance-point shift."
        + (f" No baseline for {', '.join(missing)}." if missing else "")
        + attribution
    )
    return _sim_out(day, "rule_of_thumb", units, round(total, 1), note)

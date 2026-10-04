"""Floor coupling: how much the main floor's temperature drives upstairs runtime.

Two views, both with a negative control:

- ``coupling``: an hourly regression of upstairs cooling minutes on how far the main floor
  sits above the upstairs, controlling for outdoor heat, sun and hour of day. The same model
  on the bed wing (which the floors don't feed) is the placebo.
- ``natural_experiments``: afternoons when the main floor floated warm, compared with
  similar-weather afternoons when it didn't, on weather-normalized daily upstairs runtime.
  Fake event days and the bed wing are the placebos.

Inference uses OLS with Newey-West (HAC) standard errors, implemented here with numpy: the
hourly and daily residuals are autocorrelated, and ordinary standard errors would overstate
certainty. Sun, a weaker AC on hot afternoons and Smart Away all peak together, so naive
comparisons overstate coupling; the controls and placebos are what keep this honest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from scipy import stats as sstats
from sqlalchemy.orm import Session

from climate.analytics.baseline import expected_covered_seconds, expected_seconds, fit_window
from climate.analytics.daily import (
    DayRow,
    daily_rows,
    full_day_seconds,
    hourly_outdoor,
    hourly_runtime,
    house_tz,
    is_complete,
)
from climate.api.schemas import Coupling, CouplingPoint, NaturalEvent, NaturalExperiments
from climate.store.app_settings import ControlSettings, get_setting
from climate.timeutil import day_bounds_utc, local_date, utcnow

HAC_LAG = 3
MAX_POINTS = 2000
MIN_HOURS = 72  # hours in the regression
MIN_EXPOSED = 12  # hours with, and without, the main floor above the zone
EXPOSED_F = 0.5
MIN_SLOTS = 10  # of 12 five-minute slots for an hour to count
MIN_EVENTS = 5
SUPPORT_F = 2.0  # afternoon outdoor overlap margin for natural experiments
FLOAT_F = 2.0
FLOAT_HOURS = 2
PLACEBO_SEED = 20261004
_HOUR = 3600.0


# ---------------------------------------------------------------------------------------
# OLS with Newey-West standard errors
# ---------------------------------------------------------------------------------------


@dataclass
class OlsResult:
    beta: np.ndarray
    cov: np.ndarray
    dof: int
    columns: list[int]  # indices of the design columns kept (collinear ones dropped)

    def ci(self, j: int, conf: float = 0.90) -> tuple[float, float, float]:
        """(estimate, low, high) for the coefficient of original column j."""
        k = self.columns.index(j)
        b, se = float(self.beta[k]), math.sqrt(max(float(self.cov[k, k]), 0.0))
        t = float(sstats.t.ppf(0.5 + conf / 2.0, max(self.dof, 1)))
        return b, b - t * se, b + t * se


def independent_columns(X: np.ndarray, protect: int = 0, tol: float = 1e-8) -> list[int] | None:
    """Greedy Gram-Schmidt: keep each column that adds rank, in order. Returns None when one
    of the first ``protect`` columns is (numerically) dependent on earlier ones."""
    n, k = X.shape
    basis: list[np.ndarray] = []
    keep: list[int] = []
    for j in range(k):
        norm = float(np.linalg.norm(X[:, j]))
        if norm == 0.0:
            if j < protect:
                return None
            continue
        v = X[:, j] / norm
        for _ in range(2):  # re-orthogonalize once for stability
            for q in basis:
                v = v - (q @ v) * q
        r = float(np.linalg.norm(v))
        if r > tol * math.sqrt(n):
            basis.append(v / r)
            keep.append(j)
        elif j < protect:
            return None
    return keep


def ols_hac(X: np.ndarray, y: np.ndarray, t: np.ndarray, lag: int = HAC_LAG, protect: int = 2) -> OlsResult | None:
    """OLS with Newey-West (Bartlett kernel) covariance. ``t`` are integer time indexes (hours
    or days); gaps are honored by placing scores on a dense time axis (missing times score 0),
    so lag l always means l time steps. Small-sample factor n/(n-k)."""
    cols = independent_columns(X, protect)
    if cols is None:
        return None
    Xk = X[:, cols]
    n, k = Xk.shape
    if n <= k + 1:
        return None
    xtx_inv = np.linalg.inv(Xk.T @ Xk)
    beta = xtx_inv @ (Xk.T @ y)
    u = y - Xk @ beta
    s = Xk * u[:, None]
    S = s.T @ s
    if lag > 0:
        ti = np.asarray(t, dtype=np.int64)
        ti = ti - ti.min()
        dense = np.zeros((int(ti.max()) + 1, k))
        np.add.at(dense, ti, s)
        for lg in range(1, lag + 1):
            if lg >= dense.shape[0]:
                break
            G = dense[lg:].T @ dense[:-lg]
            S += (1.0 - lg / (lag + 1.0)) * (G + G.T)
    cov = xtx_inv @ S @ xtx_inv * (n / (n - k))
    return OlsResult(beta=beta, cov=cov, dof=n - k, columns=cols)


# ---------------------------------------------------------------------------------------
# hourly panel
# ---------------------------------------------------------------------------------------


def _local(epochs: np.ndarray, tz: str) -> tuple[np.ndarray, np.ndarray]:
    """(local hour of day, local date ordinal) for UTC epoch seconds."""
    z = ZoneInfo(tz)
    hours = np.empty(epochs.size, dtype=np.int64)
    days = np.empty(epochs.size, dtype=np.int64)
    for i, e in enumerate(epochs):
        d = datetime.fromtimestamp(float(e), z)
        hours[i], days[i] = d.hour, d.toordinal()
    return hours, days


@dataclass
class _Panel:
    hours: np.ndarray  # epoch seconds of each UTC hour on the grid
    local_hour: np.ndarray
    local_day: np.ndarray
    outdoor: np.ndarray
    shortwave: np.ndarray
    cool: dict[str, np.ndarray]
    heat: dict[str, np.ndarray]
    slots: dict[str, np.ndarray]
    zone: dict[str, np.ndarray]
    cool_mode: dict[str, np.ndarray]
    known_mode: dict[str, np.ndarray]


def _panel(session: Session, t0: datetime, t1: datetime, tz: str, units: list[str]) -> _Panel:
    hr = hourly_runtime(session, t0, t1, tz, units)
    out = hourly_outdoor(session, t0, t1, hr)
    start = math.floor(t0.timestamp() / _HOUR) * _HOUR
    n = max(math.ceil((t1.timestamp() - start) / _HOUR), 0)
    hours = start + _HOUR * np.arange(n)
    lh, ld = _local(hours, tz)

    def dense(unit: str, arr: np.ndarray) -> np.ndarray:
        m = hr.unit == unit
        idx = np.rint((hr.hour[m] - start) / _HOUR).astype(np.int64)
        ok = (idx >= 0) & (idx < n)
        return np.bincount(idx[ok], weights=arr[m][ok], minlength=n)[:n]

    cool, heat, slots, zone, cmode, kmode = {}, {}, {}, {}, {}, {}
    for u in units:
        cool[u] = dense(u, hr.cool)
        heat[u] = dense(u, hr.heat + hr.aux)
        slots[u] = dense(u, hr.slots)
        have = ~np.isnan(hr.zone)
        zw = dense(u, np.where(have, hr.zone * hr.slots, 0.0))
        zn = dense(u, np.where(have, hr.slots, 0.0))
        zone[u] = np.divide(zw, zn, out=np.full(n, np.nan), where=zn > 0)
        cmode[u] = dense(u, hr.cool_mode_slots)
        kmode[u] = dense(u, hr.known_mode_slots)
    return _Panel(hours, lh, ld, out.at(hours, "temp"), out.at(hours, "shortwave"), cool, heat, slots, zone, cmode, kmode)


def _cooling_hours(p: _Panel, unit: str, source: str) -> np.ndarray:
    """Hours usable for the cooling regression of ``unit`` driven by ``source``."""
    s_u, s_s = p.slots[unit], p.slots[source]
    can_cool = (p.known_mode[unit] == 0) | (p.cool_mode[unit] >= 0.5 * s_u)
    return (
        (s_u >= MIN_SLOTS) & (s_s >= MIN_SLOTS) & np.isfinite(p.zone[unit]) & np.isfinite(p.zone[source])
        & np.isfinite(p.outdoor) & (p.heat[unit] == 0) & can_cool
    )


@dataclass
class _Reg:
    coef: float
    low: float
    high: float
    n: int
    mask: np.ndarray  # hours used
    y: np.ndarray  # minutes per hour on those hours
    used_sun: bool


def _hourly_regression(p: _Panel, unit: str, source: str) -> tuple[_Reg | None, str]:
    """unit_min/h ~ 1 + (T_source - T_unit)+ + (T_out - 65)+ + sun + hour-of-day dummies."""
    m = _cooling_hours(p, unit, source)
    sw_ok = np.isfinite(p.shortwave)
    used_sun = bool(m.sum()) and float((m & sw_ok).sum()) >= 0.8 * float(m.sum())
    if used_sun:
        m = m & sw_ok
    n = int(m.sum())
    if n < MIN_HOURS:
        return None, f"only {n} usable cooling hours (needs {MIN_HOURS})"
    y = p.cool[unit][m] * (12.0 / p.slots[unit][m]) / 60.0
    x = np.clip(p.zone[source][m] - p.zone[unit][m], 0.0, None)
    exposed = int((x > EXPOSED_F).sum())
    if exposed < MIN_EXPOSED or n - exposed < MIN_EXPOSED:
        return None, (f"the main floor was more than {EXPOSED_F}°F warmer in {exposed} of {n} hours; the comparison "
                      f"needs at least {MIN_EXPOSED} hours each way")
    cols = [np.ones(n), x, np.clip(p.outdoor[m] - 65.0, 0.0, None)]
    if used_sun:
        cols.append(p.shortwave[m] / 100.0)
    lh = p.local_hour[m]
    cols += [(lh == h).astype(float) for h in range(1, 24)]
    X = np.column_stack(cols)
    t = np.rint(p.hours[m] / _HOUR).astype(np.int64)
    res = ols_hac(X, y, t, HAC_LAG, protect=2)
    if res is None:
        return None, "the temperature difference can't be separated from the other controls"
    b, lo, hi = res.ci(1)
    return _Reg(b, lo, hi, n, m, y, used_sun), ""


def _downsample(n: int, k: int = MAX_POINTS) -> np.ndarray:
    if n <= k:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, k).round().astype(np.int64))


def _excludes_zero(lo: float, hi: float) -> bool:
    return lo > 0 or hi < 0


def coupling(session: Session, days: int = 30) -> Coupling:
    """Hourly regression on cooling hours: up_runtime_min ~ a + b*(T_main - T_up)+ +
    c*(T_out - 65)+ + d*shortwave + hour-of-day dummies. Report b with a 90% interval
    (HAC/Newey-West SE, lag 3). Placebo: same model for the bed wing using (T_main - T_bed)+;
    should be ~0. Downsample points to <= 2000 for the scatter."""
    tz = house_tz(session)
    t1 = utcnow().replace(minute=0, second=0, microsecond=0)
    t0 = t1 - timedelta(days=days)
    p = _panel(session, t0, t1, tz, ["main", "up", "bed"])
    up, why = _hourly_regression(p, "up", "main")
    bed, why_bed = _hourly_regression(p, "bed", "main")

    points: list[CouplingPoint] = []
    m_pts = _cooling_hours(p, "up", "main")
    if up is not None:
        m_pts = up.mask
    idx = np.flatnonzero(m_pts)
    for i in idx[_downsample(idx.size)]:
        sl = p.slots["up"][i]
        duty = min(100.0, p.cool["up"][i] / (sl * 300.0) * 100.0) if sl else 0.0
        sw = p.shortwave[i]
        points.append(CouplingPoint(
            ts=datetime.fromtimestamp(float(p.hours[i]), UTC),
            main_minus_up_f=round(float(p.zone["main"][i] - p.zone["up"][i]), 2),
            up_duty_pct=round(duty, 1),
            outdoor_f=round(float(p.outdoor[i]), 1) if np.isfinite(p.outdoor[i]) else None,
            shortwave_wm2=round(float(sw), 0) if np.isfinite(sw) else None,
        ))

    if up is None:
        return Coupling(days=days, n_hours=int(m_pts.sum()), points=points, coef_min_per_degf=None, ci90=None,
                        placebo_coef=None, placebo_ci90=None,
                        interpretation=f"Not enough data to measure floor coupling over {days} days: {why}.")
    text = (f"Each 1°F the main floor sits above the upstairs adds about {up.coef:.2f} min of upstairs cooling per "
            f"hour (90% interval {up.low:.2f} to {up.high:.2f}), after outdoor heat"
            f"{', sun' if up.used_sun else ''} and time of day.")
    if not _excludes_zero(up.low, up.high):
        text = (f"No clear coupling: the upstairs effect is {up.coef:+.2f} min/h per °F (90% interval {up.low:.2f} "
                f"to {up.high:.2f}), which includes zero.")
    if bed is None:
        text += f" The bed-wing placebo couldn't be run ({why_bed})."
    elif _excludes_zero(bed.low, bed.high):
        text += (f" Caution: the bed-wing placebo also moves ({bed.coef:+.2f}, 90% interval {bed.low:.2f} to "
                 f"{bed.high:.2f}), so something else that tracks the main floor may be at work.")
    else:
        text += f" The bed-wing placebo is {bed.coef:+.2f} ({bed.low:.2f} to {bed.high:.2f}), consistent with no effect."
    return Coupling(
        days=days, n_hours=up.n, points=points, coef_min_per_degf=round(up.coef, 3),
        ci90=(round(up.low, 3), round(up.high, 3)),
        placebo_coef=None if bed is None else round(bed.coef, 3),
        placebo_ci90=None if bed is None else (round(bed.low, 3), round(bed.high, 3)),
        interpretation=text,
    )


# ---------------------------------------------------------------------------------------
# natural experiments
# ---------------------------------------------------------------------------------------


def _day_effect(resid_min: dict[int, float], event: dict[int, bool], aft_out: dict[int, float],
                aft_sw: dict[int, float]) -> tuple[float, float, float] | None:
    """Regression-adjusted event effect on daily residual minutes, HAC (lag 3 days) 90% CI."""
    days = sorted(d for d in resid_min if d in event and d in aft_out)
    n_ev = sum(event[d] for d in days)
    if n_ev < MIN_EVENTS or len(days) - n_ev < MIN_EVENTS:
        return None
    y = np.array([resid_min[d] for d in days])
    ev = np.array([1.0 if event[d] else 0.0 for d in days])
    to = np.array([aft_out[d] for d in days])
    cols = [np.ones(len(days)), ev, to - to.mean()]
    sw = np.array([aft_sw.get(d, np.nan) for d in days])
    if np.isfinite(sw).all():
        cols.append((sw - sw.mean()) / 100.0)
    res = ols_hac(np.column_stack(cols), y, np.array(days), HAC_LAG, protect=2)
    return None if res is None else res.ci(1)


def natural_experiments(session: Session, days: int = 90) -> NaturalExperiments:
    """Past afternoons where the main floor floated warm (>= 2°F above its occupied cool
    setpoint for >= 2 h between 12:00 and 18:00 local) vs similar-weather afternoons without:
    difference in upstairs residual runtime (actual - baseline). Placebo with fake event days,
    and the bed wing as a negative control.

    The baseline here is fitted on the window itself (a weather-normalization device, not a
    savings claim). Only days whose afternoon outdoor temperature lies within 2°F of the other
    group's range are compared (common support), and the difference is regression-adjusted for
    afternoon temperature and sun with a Newey-West interval."""
    tz = house_tz(session)
    today = local_date(utcnow(), tz)
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    t0, t1 = day_bounds_utc(start, tz)[0], day_bounds_utc(end, tz)[1]
    control = get_setting(session, "control", ControlSettings)
    band = control.comfort.get("main")
    setpoint = band.day.cool_f if band else 76.0

    p = _panel(session, t0, t1, tz, ["main"])
    afternoon = (p.local_hour >= 12) & (p.local_hour < 18)
    have_zone = np.isfinite(p.zone["main"]) & (p.slots["main"] >= 6)
    over = p.zone["main"] - setpoint
    aft_out: dict[int, float] = {}
    aft_sw: dict[int, float] = {}
    float_hours: dict[int, int] = {}
    float_f: dict[int, float] = {}
    for d in np.unique(p.local_day[afternoon]):
        m = afternoon & (p.local_day == d)
        o = p.outdoor[m]
        if np.isfinite(o).sum() >= 4:
            aft_out[int(d)] = float(np.nanmean(o))
        s = p.shortwave[m]
        if np.isfinite(s).sum() >= 4:
            aft_sw[int(d)] = float(np.nanmean(s))
        mz = m & have_zone
        if mz.sum() >= 4:
            hot = mz & (over >= FLOAT_F)
            float_hours[int(d)] = int(hot.sum())
            float_f[int(d)] = float(over[hot].mean()) if hot.any() else 0.0

    rows = daily_rows(session, start, end, tz, ["up", "bed"])
    fits = fit_window(session, start, end, tz, rows=rows)
    by: dict[str, dict[int, DayRow]] = {"up": {}, "bed": {}}
    for r in rows:
        if r.unit_key in by and is_complete(r):
            by[r.unit_key][r.day.toordinal()] = r

    def residuals(unit: str) -> dict[int, float]:
        fit = fits.get((unit, "cool"))
        if fit is None:
            return {}
        out: dict[int, float] = {}
        for d, r in by[unit].items():
            e = expected_seconds(fit, r)
            if r.cool_s > 0 and not math.isnan(e):
                out[d] = (full_day_seconds(r, "cool") - e) / 60.0
        return out

    res_up, res_bed = residuals("up"), residuals("bed")
    cand = [d for d in sorted(res_up) if d in float_hours and d in aft_out]
    event = {d: float_hours[d] >= FLOAT_HOURS for d in cand}
    ev_days = [d for d in cand if event[d]]
    ctl_days = [d for d in cand if not event[d]]

    events_out: list[NaturalEvent] = []
    fit_up = fits.get(("up", "cool"))
    for d in ev_days:
        r = by["up"][d]
        e = expected_covered_seconds(fit_up, r) if fit_up else math.nan
        events_out.append(NaturalEvent(
            date=date.fromordinal(d), main_floor_float_f=round(float_f[d], 1), up_runtime_min=round(r.cool_s / 60.0, 1),
            expected_up_runtime_min=None if math.isnan(e) else round(e / 60.0, 1),
        ))

    def empty(note: str) -> NaturalExperiments:
        return NaturalExperiments(days=days, events=events_out, estimate_min_per_event=None, ci90=None,
                                  placebo_estimate=None, bed_wing_estimate=None, note=note)

    if fit_up is None:
        return empty(f"No upstairs cooling baseline can be fitted on these {days} days yet, so there is nothing to "
                     "compare against.")
    if len(ev_days) < MIN_EVENTS or len(ctl_days) < MIN_EVENTS:
        return empty(f"{len(ev_days)} warm-main-floor afternoons and {len(ctl_days)} comparison afternoons in {days} "
                     f"days; at least {MIN_EVENTS} of each are needed.")

    # Common support on afternoon outdoor temperature.
    ev_t = np.array([aft_out[d] for d in ev_days])
    ct_t = np.array([aft_out[d] for d in ctl_days])
    keep = [d for d in cand if (ct_t.min() - SUPPORT_F <= aft_out[d] <= ct_t.max() + SUPPORT_F) and
            (ev_t.min() - SUPPORT_F <= aft_out[d] <= ev_t.max() + SUPPORT_F)]
    ev_k = [d for d in keep if event[d]]
    eff = _day_effect({d: res_up[d] for d in keep}, event, aft_out, aft_sw)
    if eff is None:
        return empty(f"Only {len(ev_k)} warm-main-floor afternoons have comparison afternoons with similar weather "
                     f"(within {SUPPORT_F:.0f}°F); at least {MIN_EVENTS} of each are needed.")
    est, lo, hi = eff

    # Placebo 1: fake events drawn from the comparison days only (seeded, reproducible).
    ctl_k = [d for d in keep if not event[d]]
    rng = np.random.default_rng(PLACEBO_SEED)
    fake = set(np.array(ctl_k)[rng.permutation(len(ctl_k))[: len(ctl_k) // 2]].tolist()) if ctl_k else set()
    placebo = _day_effect({d: res_up[d] for d in ctl_k}, {d: d in fake for d in ctl_k}, aft_out, aft_sw)
    # Placebo 2: the bed wing on the same days (the floors don't feed it).
    bed = _day_effect({d: res_bed[d] for d in keep if d in res_bed}, event, aft_out, aft_sw)

    note = (f"On {len(ev_k)} afternoons the main floor floated {FLOAT_F:.0f}°F+ above its occupied setpoint for "
            f"{FLOAT_HOURS}+ hours; against {len(keep) - len(ev_k)} similar-weather afternoons the upstairs ran "
            f"{est:+.0f} min per day versus its weather baseline (90% interval {lo:.0f} to {hi:.0f}).")
    if not _excludes_zero(lo, hi):
        note = note[:-1] + ", which includes zero: no clear effect."
    checks = []
    if placebo is not None:
        checks.append(f"fake-event placebo {placebo[0]:+.0f} min ({placebo[1]:.0f} to {placebo[2]:.0f})")
    if bed is not None:
        checks.append(f"bed wing {bed[0]:+.0f} min ({bed[1]:.0f} to {bed[2]:.0f})")
    if checks:
        bad = (placebo is not None and _excludes_zero(placebo[1], placebo[2])) or (
            bed is not None and _excludes_zero(bed[1], bed[2]))
        note += " Checks: " + "; ".join(checks) + (
            " - a check moved, so treat the estimate with suspicion." if bad else ", both consistent with no effect.")
    return NaturalExperiments(
        days=days, events=events_out, estimate_min_per_event=round(est, 1), ci90=(round(lo, 1), round(hi, 1)),
        placebo_estimate=None if placebo is None else round(placebo[0], 1),
        bed_wing_estimate=None if bed is None else round(bed[0], 1), note=note,
    )

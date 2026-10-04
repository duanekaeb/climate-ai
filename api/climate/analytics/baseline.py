"""Weather baselines per unit and mode (CalTRACK-style).

runtime_s/day = intercept + slope * DD(bp), bp searched over 30-90°F in 1°F steps (only bps
leaving >= 10 days with DD > 0), least squares, keep the best adjusted R². Checks:
CV(RMSE) <= 20% and |NMBE| <= 0.5% (ASHRAE Guideline 14 daily). Needs >= 21 days.

Details that matter for honesty:

- Only complete days are fitted (``daily.is_complete``: >= 90% of slots and outdoor hours) and
  days with the system switched off for over half the day are left out. A >= 90% day is scaled
  up to its full length (``daily.full_day_seconds``); DST days keep their 23/25 hours on both
  sides (runtime and degree-hours), so no other normalization is needed.
- A mode needs >= 10 days with runtime in that mode, otherwise there is nothing to model.
- The intercept is constrained to >= 0 (the constrained optimum then lies on intercept = 0)
  and the slope must be positive.
- The balance point is often weakly identified (a summer of days that are all above a range
  of balance points fits that whole range equally well; a narrow weather range lets a high
  balance point chase noise). Grid points inside the 90% profile-likelihood set of the best
  one (n * ln(SSE / SSE_min) <= chi2(1, 0.90) = 2.71) are statistically indistinguishable, and
  among them we keep the smallest non-negative intercept: as much runtime as the data allows
  is attributed to weather (an HVAC unit barely runs on a day with no degree-days). Plain max
  adjusted R² is the special case of a one-point set; the fit's notes name the set.
- Every fit keeps its residual std and lag-1 autocorrelation for the savings interval
  (ASHRAE G14 fractional savings uncertainty, ``ResidualStats``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from scipy import stats as sstats
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from climate.analytics.daily import (
    DayRow,
    daily_rows,
    day_degree_days,
    degree_days_grid,
    full_day_seconds,
    house_tz,
    is_complete,
    mode_seconds,
    unit_order,
)
from climate.api.schemas import BaselineOut, DailyRuntime
from climate.store.orm import ModelFit
from climate.timeutil import local_date, utcnow

MODES = ("cool", "heat")
BP_GRID = np.arange(30.0, 91.0, 1.0)
MIN_DAYS = 21
MIN_DD_DAYS = 10
MIN_RUNTIME_DAYS = 10
N_PARAMS = 2
PROFILE_LR_90 = float(sstats.chi2.ppf(0.90, 1))  # 2.706
TRAIN_DAYS = 90
G14_FACTOR = 1.26  # Reddy & Claridge empirical factor in the G14 savings-uncertainty formula
CV_MAX = 0.20
NMBE_MAX = 0.005


@dataclass
class BaselineFit:
    unit_key: str
    mode: str
    balance_point_f: float
    intercept_s: float
    slope_s_per_dd: float
    n_days: int
    r2: float
    cvrmse: float
    nmbe: float
    resid_std_s: float
    resid_lag1: float  # autocorrelation of residuals, for the savings interval
    train_start: date
    train_end: date
    # Extras (defaults keep the original constructor working):
    adj_r2: float | None = None
    notes: str | None = None
    fit_id: int | None = None  # model_fits.id when loaded from the database
    fitted_at: datetime | None = None
    # In-memory only: day -> (actual full-day seconds, fitted seconds) for the training days.
    daily: dict[date, tuple[float, float]] | None = field(default=None, repr=False, compare=False)

    @property
    def passes(self) -> bool:
        return self.cvrmse <= CV_MAX and abs(self.nmbe) <= NMBE_MAX

    @property
    def mean_y_s(self) -> float:
        if self.daily:
            return float(np.mean([a for a, _ in self.daily.values()]))
        return self.resid_std_s / self.cvrmse if self.cvrmse > 0 else 0.0

    def check_summary(self) -> str:
        return f"CV(RMSE) {self.cvrmse * 100:.1f}% (max 20%), NMBE {self.nmbe * 100:+.2f}% (max ±0.5%)"


# ---------------------------------------------------------------------------------------
# fitting
# ---------------------------------------------------------------------------------------


def fit_day_ok(row: DayRow) -> bool:
    """Days a baseline may learn from: complete, and the system not off for most of it."""
    return is_complete(row) and row.off_slots <= 0.5 * row.slots


def _lag1(ordinals: np.ndarray, r: np.ndarray) -> float:
    """Lag-1 autocorrelation over pairs of consecutive calendar days (gaps are skipped)."""
    if r.size < 3:
        return 0.0
    rc = r - r.mean()
    consec = np.diff(ordinals) == 1
    if consec.sum() < 2:
        return 0.0
    a, b = rc[:-1][consec], rc[1:][consec]
    den = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 0 else 0.0


def fit_baseline(days: list[DayRow], mode: str) -> BaselineFit | None:
    """Fit one unit's daily runtime for one mode. None when the data can't support a model
    (< 21 complete days, < 10 days with runtime in this mode, or no balance point leaving
    >= 10 days with DD > 0 and a positive slope). A returned fit may still fail its checks
    (``passes``); callers must look before claiming anything with it."""
    if mode not in MODES:
        raise ValueError(f"mode must be 'cool' or 'heat', not {mode!r}")
    units = {d.unit_key for d in days}
    if len(units) > 1:
        raise ValueError(f"fit_baseline takes one unit's rows, got {sorted(units)}")
    rows = sorted({d.day: d for d in days if fit_day_ok(d)}.values(), key=lambda r: r.day)
    if len(rows) < MIN_DAYS:
        return None
    y = np.array([full_day_seconds(r, mode) for r in rows])
    if int((y > 0).sum()) < MIN_RUNTIME_DAYS or y.mean() <= 0:
        return None

    X = degree_days_grid(rows, BP_GRID, mode)  # (n, k)
    n = y.size
    ym = y.mean()
    xm = X.mean(axis=0)
    xc = X - xm
    sxx = (xc * xc).sum(axis=0)
    sxy = (xc * (y - ym)[:, None]).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(sxx > 0, sxy / sxx, np.nan)
        icpt = ym - slope * xm
        # intercept >= 0: when the unconstrained optimum is negative, the constrained
        # least-squares optimum lies on the boundary intercept = 0 (fit through the origin).
        sxx0 = (X * X).sum(axis=0)
        slope0 = np.where(sxx0 > 0, (X * y[:, None]).sum(axis=0) / sxx0, np.nan)
    pinned = icpt < 0
    slope = np.where(pinned, slope0, slope)
    icpt = np.where(pinned, 0.0, icpt)
    resid = y[:, None] - (icpt + slope * X)
    sse = (resid * resid).sum(axis=0)
    valid = ((X > 0).sum(axis=0) >= MIN_DD_DAYS) & (sxx > 0) & (slope > 0) & np.isfinite(sse)
    if not valid.any():
        return None
    tiny = 1e-9 * max(float((y * y).sum()), 1.0)
    best = max(float(sse[valid].min()), tiny)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = n * np.log(np.maximum(sse, tiny) / best)
    ties = np.flatnonzero(valid & (lr <= PROFILE_LR_90))
    j = int(ties[np.lexsort((sse[ties], icpt[ties]))[0]])  # smallest intercept, then SSE

    b0, b1, bp = float(icpt[j]), float(slope[j]), float(BP_GRID[j])
    fitted = b0 + b1 * X[:, j]
    r = y - fitted
    sse_j = float((r * r).sum())
    sst = float(((y - ym) ** 2).sum())
    r2 = 1.0 - sse_j / sst if sst > 0 else 0.0
    dof = max(n - N_PARAMS, 1)
    adj = 1.0 - (1.0 - r2) * (n - 1) / dof
    std = math.sqrt(sse_j / dof)
    ordinals = np.array([d.day.toordinal() for d in rows])
    notes = [f"{n} complete days; bp {bp:.0f}°F"]
    if pinned[j]:
        notes.append("intercept constrained to 0")
    tie_bps = BP_GRID[ties]
    if tie_bps.size > 1:
        notes.append(
            f"90% balance-point set {tie_bps.min():.0f}-{tie_bps.max():.0f}°F ({tie_bps.size} candidates); "
            "kept the smallest non-negative intercept"
        )
    return BaselineFit(
        unit_key=rows[0].unit_key,
        mode=mode,
        balance_point_f=bp,
        intercept_s=b0,
        slope_s_per_dd=b1,
        n_days=n,
        r2=r2,
        cvrmse=std / ym,
        nmbe=float(r.sum()) / (dof * ym),
        resid_std_s=std,
        resid_lag1=_lag1(ordinals, r),
        train_start=rows[0].day,
        train_end=rows[-1].day,
        adj_r2=adj,
        notes="; ".join(notes),
        daily={d.day: (float(a), float(f)) for d, a, f in zip(rows, y, fitted, strict=True)},
    )


def expected_seconds(fit: BaselineFit, day: DayRow) -> float:
    """Expected stage-1 seconds for the FULL local day (NaN when the day has no outdoor data).
    For a partial day multiply by ``day.coverage`` (see ``expected_covered_seconds``)."""
    dd = day_degree_days(day, fit.balance_point_f, fit.mode)
    if dd is None:
        return math.nan
    return fit.intercept_s + fit.slope_s_per_dd * dd


def expected_covered_seconds(fit: BaselineFit, day: DayRow) -> float:
    """Expected seconds over the slots the day actually has data for (comparable to cool_s /
    heat_s of the same row)."""
    return expected_seconds(fit, day) * min(day.coverage, 1.0)


def weather_part_seconds(fit: BaselineFit, day: DayRow) -> float:
    """The degree-day-driven part of the expectation (slope * DD), 0 without outdoor data."""
    dd = day_degree_days(day, fit.balance_point_f, fit.mode)
    return 0.0 if dd is None else fit.slope_s_per_dd * dd


INVOLVED_SHARE = 0.05


def involved_modes(rows: list[DayRow], fits: dict[tuple[str, str], BaselineFit]) -> set[tuple[str, str]]:
    """(unit, mode) pairs that matter for a period: the mode's actual runtime, or its
    baseline's degree-day-driven expectation, is >= 5% of that unit's runtime in the rows.
    Keeps an out-of-season model (say a heating fit on summer days, which would predict only
    its intercept) from manufacturing savings or drift."""
    actual: dict[tuple[str, str], float] = defaultdict(float)
    weather: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        for m in MODES:
            key = (r.unit_key, m)
            actual[key] += mode_seconds(r, m)
            if key in fits:
                weather[key] += weather_part_seconds(fits[key], r)
    out: set[tuple[str, str]] = set()
    for u in {r.unit_key for r in rows}:
        total = max(sum(actual[(u, m)] for m in MODES), sum(weather[(u, m)] for m in MODES), 1.0)
        for m in MODES:
            if actual[(u, m)] >= INVOLVED_SHARE * total or weather[(u, m)] >= INVOLVED_SHARE * total:
                out.add((u, m))
    return out


# ---------------------------------------------------------------------------------------
# uncertainty (ASHRAE Guideline 14 with autocorrelation)
# ---------------------------------------------------------------------------------------


@dataclass
class ResidualStats:
    """Daily residual spread of a (possibly power-weighted, multi-unit) baseline."""

    sigma_s: float  # residual std per day (seconds)
    rho: float  # lag-1 autocorrelation, clipped to [0, 0.9]
    n: int  # training days
    p: int  # fitted parameters
    mean_y_s: float  # mean daily actual over the training days

    @property
    def n_eff(self) -> float:
        return self.n * (1.0 - self.rho) / (1.0 + self.rho)

    @property
    def cv(self) -> float:
        return self.sigma_s / self.mean_y_s if self.mean_y_s > 0 else math.inf

    def t(self, conf: float = 0.90) -> float:
        return float(sstats.t.ppf(0.5 + conf / 2.0, max(self.n_eff - self.p, 1.0)))

    def halfwidth(self, m: int, mean_expected_day_s: float | None = None, conf: float = 0.90) -> float:
        """Half-width (seconds) of the interval on a sum of m reporting days of
        (expected - actual): ASHRAE G14 / Reddy-Claridge,
        t * 1.26 * sigma * sqrt(m * (n/n') * (1 + 2/n')), n' = n(1-rho)/(1+rho).
        G14 scales sigma with the reporting period's level (CV * mean expected); we use the
        larger of that and the absolute sigma, so neither a hot nor a mild period narrows it."""
        if m <= 0:
            return 0.0
        n_eff = max(self.n_eff, 1.0)
        scale = 1.0
        if mean_expected_day_s is not None and self.mean_y_s > 0:
            scale = max(1.0, mean_expected_day_s / self.mean_y_s)
        return self.t(conf) * G14_FACTOR * self.sigma_s * scale * math.sqrt(m * (self.n / n_eff) * (1.0 + 2.0 / n_eff))


def _clip_rho(rho: float) -> float:
    return min(max(rho, 0.0), 0.9)


def residual_stats(fits: Iterable[tuple[BaselineFit, float]]) -> ResidualStats | None:
    """Combine weighted fits into one daily residual series on their common training days
    (captures cross-unit correlation). Without enough common days (or without in-memory
    training data), combine per-fit spreads assuming independence."""
    fits = [(f, w) for f, w in fits if w > 0]
    if not fits:
        return None
    p = N_PARAMS * len(fits)
    if all(f.daily for f, _ in fits):
        common = sorted(set.intersection(*(set(f.daily or {}) for f, _ in fits)))
        if len(common) >= max(MIN_DAYS, p + 5):
            r = np.zeros(len(common))
            yv = np.zeros(len(common))
            for f, w in fits:
                d = f.daily or {}
                a = np.array([d[k][0] for k in common])
                fh = np.array([d[k][1] for k in common])
                r += w * (a - fh)
                yv += w * a
            n = len(common)
            sigma = math.sqrt(float((r * r).sum()) / max(n - p, 1))
            ords = np.array([k.toordinal() for k in common])
            return ResidualStats(sigma, _clip_rho(_lag1(ords, r)), n, p, float(yv.mean()))
    sigma = math.sqrt(sum((w * f.resid_std_s) ** 2 for f, w in fits))
    return ResidualStats(
        sigma_s=sigma,
        rho=_clip_rho(max(f.resid_lag1 for f, _ in fits)),
        n=min(f.n_days for f, _ in fits),
        p=N_PARAMS,
        mean_y_s=sum(w * f.mean_y_s for f, w in fits),
    )


# ---------------------------------------------------------------------------------------
# training windows, storage
# ---------------------------------------------------------------------------------------

_TREATMENT_SQL = text(
    """
    SELECT ed.day
    FROM experiment_days ed
    JOIN experiments e ON e.id = ed.experiment_id
    WHERE e.status = 'running'
      AND ed.day BETWEEN :s AND :e
      AND ed.arm IS DISTINCT FROM (e.arms -> 0 ->> 'key')
    """
)


def treatment_days(session: Session, start: date, end: date) -> set[date]:
    """Days in [start, end] assigned to a running experiment's treatment arm (any arm other
    than the first). Baselines never learn from them."""
    return set(session.execute(_TREATMENT_SQL, {"s": start, "e": end}).scalars())


def fit_window(
    session: Session, start: date, end: date, tz: str | None = None, unit_keys: list[str] | None = None,
    rows: list[DayRow] | None = None,
) -> dict[tuple[str, str], BaselineFit]:
    """Fit every unit x mode on the local days [start, end] (treatment-arm days excluded).
    Not stored. ``rows`` may be passed when the caller already has them."""
    if end < start:
        return {}
    tz = tz or house_tz(session)
    rows = rows if rows is not None else daily_rows(session, start, end, tz, unit_keys)
    skip = treatment_days(session, start, end)
    by_unit: dict[str, list[DayRow]] = defaultdict(list)
    for r in rows:
        if start <= r.day <= end and r.day not in skip:
            by_unit[r.unit_key].append(r)
    out: dict[tuple[str, str], BaselineFit] = {}
    for unit in sorted(by_unit, key=unit_order):
        for mode in MODES:
            fit = fit_baseline(by_unit[unit], mode)
            if fit is not None:
                out[(unit, mode)] = fit
    return out


def pre_period_fits(session: Session, before: date, tz: str | None = None, train_days: int = TRAIN_DAYS) -> dict[tuple[str, str], BaselineFit]:
    """Baselines trained on the ``train_days`` local days strictly before ``before``: the
    out-of-sample baseline for any claim about days from ``before`` on (IPMVP: the reporting
    period never trains its own baseline)."""
    end = before - timedelta(days=1)
    return fit_window(session, end - timedelta(days=train_days - 1), end, tz)


def _r(x: float, nd: int = 4) -> float:
    return float(f"{x:.{nd}g}") if math.isfinite(x) else x


def refit_all(session: Session, now: datetime, train_days: int = 90) -> list[int]:
    """Fit every unit x mode with enough data on the last train_days (excluding days used by a
    running experiment's treatment arm), store model_fits (kind='baseline') as 'active',
    retiring the previous active fit. Returns new ids."""
    tz = house_tz(session)
    end = local_date(now, tz) - timedelta(days=1)  # the last finished local day
    fits = fit_window(session, end - timedelta(days=train_days - 1), end, tz)
    ids: list[int] = []
    for (unit, mode), fit in fits.items():
        session.execute(
            update(ModelFit)
            .where(ModelFit.kind == "baseline", ModelFit.unit_key == unit, ModelFit.mode == mode,
                   ModelFit.status == "active")
            .values(status="retired")
        )
        row = ModelFit(
            kind="baseline",
            unit_key=unit,
            mode=mode,
            train_start=fit.train_start,
            train_end=fit.train_end,
            params={
                "balance_point_f": fit.balance_point_f,
                "intercept_s": _r(fit.intercept_s, 6),
                "slope_s_per_dd": _r(fit.slope_s_per_dd, 6),
                "resid_std_s": _r(fit.resid_std_s, 6),
                "resid_lag1": _r(fit.resid_lag1, 4),
            },
            metrics={
                "r2": _r(fit.r2, 4),
                "adj_r2": _r(fit.adj_r2 if fit.adj_r2 is not None else fit.r2, 4),
                "cvrmse": _r(fit.cvrmse, 4),
                "nmbe": _r(fit.nmbe, 4),
                "n_days": fit.n_days,
                "passes": fit.passes,
            },
            status="active",
            notes=fit.notes,
        )
        session.add(row)
        session.flush()
        ids.append(int(row.id))
    return ids


def fit_from_row(row: ModelFit) -> BaselineFit | None:
    """BaselineFit from a stored model_fits row (None if the row is malformed)."""
    p: dict[str, Any] = row.params or {}
    m: dict[str, Any] = row.metrics or {}
    try:
        return BaselineFit(
            unit_key=str(row.unit_key),
            mode=str(row.mode),
            balance_point_f=float(p["balance_point_f"]),
            intercept_s=float(p["intercept_s"]),
            slope_s_per_dd=float(p["slope_s_per_dd"]),
            n_days=int(m.get("n_days", 0)),
            r2=float(m.get("r2", 0.0)),
            cvrmse=float(m.get("cvrmse", math.inf)),
            nmbe=float(m.get("nmbe", math.inf)),
            resid_std_s=float(p.get("resid_std_s", 0.0)),
            resid_lag1=float(p.get("resid_lag1", 0.0)),
            train_start=row.train_start or date.min,
            train_end=row.train_end or date.min,
            adj_r2=float(m["adj_r2"]) if "adj_r2" in m else None,
            notes=row.notes,
            fit_id=int(row.id),
            fitted_at=row.created_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


def active_fits(session: Session) -> dict[tuple[str, str], BaselineFit]:
    """(unit_key, mode) -> the active baseline fit."""
    q = (
        select(ModelFit)
        .where(ModelFit.kind == "baseline", ModelFit.status == "active")
        .order_by(ModelFit.created_at.desc(), ModelFit.id.desc())
    )
    out: dict[tuple[str, str], BaselineFit] = {}
    for row in session.execute(q).scalars():
        key = (str(row.unit_key), str(row.mode))
        if key in out or row.mode not in MODES:
            continue
        fit = fit_from_row(row)
        if fit is not None:
            out[key] = fit
    return out


# ---------------------------------------------------------------------------------------
# wire helpers for the API layer
# ---------------------------------------------------------------------------------------


def to_out(fit: BaselineFit) -> BaselineOut:
    return BaselineOut(
        unit_key=fit.unit_key,
        mode=fit.mode,  # type: ignore[arg-type]
        balance_point_f=fit.balance_point_f,
        intercept_min=round(fit.intercept_s / 60.0, 2),
        slope_min_per_dd=round(fit.slope_s_per_dd / 60.0, 3),
        n_days=fit.n_days,
        r2=round(fit.r2, 4),
        cvrmse=round(fit.cvrmse, 4),
        nmbe=round(fit.nmbe, 5),
        passes=fit.passes,
        fitted_at=fit.fitted_at or datetime.now(UTC),
        train_start=fit.train_start,
        train_end=fit.train_end,
    )


def baselines_out(session: Session) -> list[BaselineOut]:
    """GET /analytics/baselines: the active fits in house unit order."""
    fits = active_fits(session)
    return [to_out(fits[k]) for k in sorted(fits, key=lambda k: (unit_order(k[0]), MODES.index(k[1])))]


def _day_mode(fits: dict[tuple[str, str], BaselineFit], row: DayRow) -> str | None:
    have = [m for m in MODES if (row.unit_key, m) in fits]
    if row.mode in have:
        return row.mode
    if row.mode is None and have:
        return max(have, key=lambda m: weather_part_seconds(fits[(row.unit_key, m)], row))
    return None


def daily_runtime(session: Session, days: int = 30, now: datetime | None = None) -> list[DailyRuntime]:
    """GET /runtime/daily: the last ``days`` local days (today included, partial) per unit,
    with the active baseline's expectation for the day's mode. ``expected_min`` is None for
    incomplete days (a partial day's expectation depends on WHEN the missing hours were)."""
    tz = house_tz(session)
    today = local_date(now or utcnow(), tz)
    rows = daily_rows(session, today - timedelta(days=max(days, 1) - 1), today, tz)
    fits = active_fits(session)
    out: list[DailyRuntime] = []
    for r in rows:
        mode = _day_mode(fits, r)
        expected = None
        if mode is not None and is_complete(r):
            e = expected_covered_seconds(fits[(r.unit_key, mode)], r)
            expected = None if math.isnan(e) else round(e / 60.0, 1)
        out.append(
            DailyRuntime(
                Date=r.day, unit_key=r.unit_key, cool_min=round(r.cool_s / 60.0, 1), heat_min=round(r.heat_s / 60.0, 1),
                aux_min=round(r.aux_s / 60.0, 1), fan_min=round(r.fan_s / 60.0, 1), expected_min=expected,
                mode=r.mode, outdoor_mean_f=r.outdoor_mean_f, outdoor_max_f=r.outdoor_max_f, cdd65=r.cdd65,
                hdd65=r.hdd65, maxed_min=r.maxed_min,
            )
        )
    return out

"""Grey-box 3-zone RC house model (blueprint §4) and per-room offsets.

Runs in shadow: its plans drive nothing until it beats the linked-floors rule in
walk-forward backtests. Separate cooling and heating parameter sets.

The model is the blueprint's, discretized on 15-minute slots (forward Euler, dt = 0.25 h)::

    C_up·dT_up/dt     = (T_out−T_up)/R_up + (T_main−T_up)/R_mu + k_s·max(0, T_main−T_up)
                        + a_up·Sun − Q_up·on_up + g_up
    C_main·dT_main/dt = (T_out−T_main)/R_m + (T_up−T_main)/R_mu + (T_bed−T_main)/R_mb
                        + a_m·Sun − Q_main·on_main + g_main
    C_bed·dT_bed/dt   = (T_out−T_bed)/R_b + (T_main−T_bed)/R_mb + a_b·Sun − Q_bed·on_bed + g_bed

(−Q for cooling, +Q for heating; Sun = shortwave in kW/m²; on = stage-1 on-fraction.)

Identifiability. Without a power meter heat is never measured in watts, so a capacitance and
the heat flows into it are only known as a ratio. The model is therefore fitted in RATE form:
each zone's equation divided by its own C, giving °F-per-hour coefficients that the data can
actually determine (17 of them)::

    main: ua_out = 1/(R_m·C_main)   ua_up = 1/(R_mu·C_main)   ua_bed = 1/(R_mb·C_main)
          sun = a_m/C_main          q = Q_main/C_main         gain = g_main/C_main
    up:   ua_out = 1/(R_up·C_up)    ua_main = 1/(R_mu·C_up)   k_stack = k_s/C_up
          sun = a_up/C_up           q = Q_up/C_up             gain = g_up/C_up
    bed:  ua_out = 1/(R_b·C_bed)    ua_main = 1/(R_mb·C_bed)
          sun = a_b/C_bed           q = Q_bed/C_bed           gain = g_bed/C_bed

They map one-to-one onto the blueprint's physical parameters once one scale is fixed; the
stored ``physical`` block uses C_main = 1 (heat measured in "°F of the main floor"), so the
capacitance ratios (C_up/C_main via the shared R_mu) and relative equipment capacities are
real numbers you can read. Every coefficient is constrained positive through a log
parameterization and fitted with ``scipy.optimize.least_squares`` on one-step-ahead
prediction (started from the non-negative least-squares solution).

Walk-forward: train on all but the last 7 days, then on the held-out week score 1-hour and
24-hour-ahead temperatures (simulated with the measured runtime, outdoor temperature and
sun) against persistence, and replay each held-out day through a thermostat emulation with
the measured setpoints to see how well the model reproduces daily runtime (that error is what
``climate.models.backtest`` propagates into its 90% interval). Parameters whose 90% interval
is wider than ±50% of their value are published as "not pinned down".
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from scipy.optimize import least_squares, nnls
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from climate.analytics.daily import heat_metrics
from climate.house import ROOMS
from climate.store.app_settings import LocationSettings, get_setting
from climate.store.orm import ModelFit, Unit
from climate.timeutil import local_date, to_local

ZONES: tuple[str, str, str] = ("main", "up", "bed")
ZIDX = {z: i for i, z in enumerate(ZONES)}
SLOT_S = 900
SLOT = timedelta(seconds=SLOT_S)
DT_H = SLOT_S / 3600.0
SLOTS_PER_HOUR = 4
SLOTS_PER_DAY = 96
TEST_DAYS = 7
MIN_TRAIN_STEPS = 3 * SLOTS_PER_DAY  # three days of usable one-step pairs per mode
MIN_TEST_STARTS = 24
Z90 = 1.6448536269514722
ACTIVE_MAX_RMSE_1H_F = 1.0
REL_HALF_WIDTH_LIMIT = 0.5
NIGHT_START_H, NIGHT_END_H = 21, 7  # room offsets: night = 21:00-07:00 local
MIN_OFFSET_POINTS = 12  # one hour of 5-minute readings per room and period

PARAM_NAMES: dict[str, tuple[str, ...]] = {
    "main": ("ua_out", "ua_up", "ua_bed", "sun", "q", "gain"),
    "up": ("ua_out", "ua_main", "k_stack", "sun", "q", "gain"),
    "bed": ("ua_out", "ua_main", "sun", "q", "gain"),
}
PARAM_HELP: dict[str, str] = {
    "main.ua_out": "main floor ↔ outdoors",
    "main.ua_up": "main floor ↔ upstairs (as felt by the main floor)",
    "main.ua_bed": "main floor ↔ bed wing (as felt by the main floor)",
    "main.sun": "sun on the main floor",
    "main.q": "main-floor equipment output",
    "main.gain": "main-floor internal gains",
    "up.ua_out": "upstairs ↔ outdoors",
    "up.ua_main": "upstairs ↔ main floor (as felt upstairs)",
    "up.k_stack": "extra stairwell heat when the main floor is warmer",
    "up.sun": "sun on the upstairs",
    "up.q": "upstairs equipment output",
    "up.gain": "upstairs internal gains",
    "bed.ua_out": "bed wing ↔ outdoors",
    "bed.ua_main": "bed wing ↔ main floor (as felt in the wing)",
    "bed.sun": "sun on the bed wing",
    "bed.q": "bed-wing equipment output",
    "bed.gain": "bed-wing internal gains",
}
_LOG_LO, _LOG_HI = math.log(1e-6), math.log(1e3)
_PARAM_FLOOR = 1e-4  # least-squares start for parameters NNLS put at zero


# ---------------------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RCModel:
    """Rate-form parameters for one mode. Arrays follow ``PARAM_NAMES`` order."""

    mode: str  # 'cool' | 'heat'
    main: np.ndarray
    up: np.ndarray
    bed: np.ndarray

    @property
    def sign(self) -> float:
        return -1.0 if self.mode == "cool" else 1.0

    @property
    def q(self) -> np.ndarray:
        return np.array([self.main[4], self.up[4], self.bed[3]])

    def free_rate(self, T: np.ndarray, t_out: np.ndarray, sun: np.ndarray) -> np.ndarray:
        """dT/dt (°F/h) with the equipment off. T (..., 3); t_out, sun broadcast to T[..., 0]."""
        m, u, b = self.main, self.up, self.bed
        tm, tu, tb = T[..., 0], T[..., 1], T[..., 2]
        dm = m[0] * (t_out - tm) + m[1] * (tu - tm) + m[2] * (tb - tm) + m[3] * sun + m[5]
        du = u[0] * (t_out - tu) + u[1] * (tm - tu) + u[2] * np.maximum(0.0, tm - tu) + u[3] * sun + u[5]
        db = b[0] * (t_out - tb) + b[1] * (tm - tb) + b[2] * sun + b[4]
        return np.stack([dm, du, db], axis=-1)

    def step(self, T: np.ndarray, t_out: np.ndarray, sun: np.ndarray, on: np.ndarray) -> np.ndarray:
        """One 15-minute step with measured on-fractions ``on`` (..., 3)."""
        return T + DT_H * (self.free_rate(T, t_out, sun) + self.sign * self.q * on)

    def to_params(self) -> dict[str, dict[str, float]]:
        return {
            z: {n: float(v) for n, v in zip(PARAM_NAMES[z], getattr(self, z), strict=True)} for z in ZONES
        }

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> RCModel:
        zones = params.get("zones", params)
        arrs = {z: np.array([float(zones[z][n]) for n in PARAM_NAMES[z]]) for z in ZONES}
        return cls(mode=str(params.get("mode", "cool")), main=arrs["main"], up=arrs["up"], bed=arrs["bed"])

    def physical(self) -> dict[str, Any]:
        """The blueprint's physical parameters with C_main = 1 (heat in main-floor °F)."""
        m, u, b = self.main, self.up, self.bed

        def ratio(x: float, y: float) -> float | None:
            return float(x / y) if y > 0 and math.isfinite(x / y) else None

        c_up = ratio(m[1], u[1])
        c_bed = ratio(m[2], b[1])

        def scaled(v: float, c: float | None) -> float | None:
            return None if c is None else float(v * c)

        def inv(v: float | None) -> float | None:
            return None if v is None or v <= 0 else float(1.0 / v)

        return {
            "units": "C_main = 1; R in hours·C_main per °F; a_sun per kW/m², Q and g in C_main·°F/h",
            "C": {"main": 1.0, "up": c_up, "bed": c_bed},
            "R_out": {
                "main": inv(float(m[0])),
                "up": inv(scaled(u[0], c_up)),
                "bed": inv(scaled(b[0], c_bed)),
            },
            "R_mu": inv(float(m[1])),
            "R_mb": inv(float(m[2])),
            "k_s": scaled(u[2], c_up),
            "a_sun": {"main": float(m[3]), "up": scaled(u[3], c_up), "bed": scaled(b[2], c_bed)},
            "Q": {"main": float(m[4]), "up": scaled(u[4], c_up), "bed": scaled(b[3], c_bed)},
            "g": {"main": float(m[5]), "up": scaled(u[5], c_up), "bed": scaled(b[4], c_bed)},
        }


def regressors(
    zone: str, T: np.ndarray, t_out: np.ndarray, sun: np.ndarray, on: np.ndarray, sign: float
) -> np.ndarray:
    """Columns X (n, p) such that dT_zone/dt = X @ rates[zone] (same order as PARAM_NAMES)."""
    tm, tu, tb = T[:, 0], T[:, 1], T[:, 2]
    one = np.ones_like(tm)
    if zone == "main":
        cols = [t_out - tm, tu - tm, tb - tm, sun, sign * on[:, 0], one]
    elif zone == "up":
        cols = [t_out - tu, tm - tu, np.maximum(0.0, tm - tu), sun, sign * on[:, 1], one]
    else:
        cols = [t_out - tb, tm - tb, sun, sign * on[:, 2], one]
    return np.column_stack(cols)


def simulate_measured(
    model: RCModel, T0: np.ndarray, t_out: np.ndarray, sun: np.ndarray, on: np.ndarray
) -> np.ndarray:
    """Free simulation with measured inputs. T0 (B, 3); t_out, sun (H, B); on (H, B, 3).
    Returns the temperatures after H steps, (B, 3)."""
    T = np.array(T0, dtype=float)
    for h in range(t_out.shape[0]):
        T = model.step(T, t_out[h], sun[h], on[h])
    return T


def emulate(
    model: RCModel,
    T0: np.ndarray,
    t_out: np.ndarray,
    sun: np.ndarray,
    setpoint: np.ndarray,
    off: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Thermostat emulation: each slot the unit runs just enough (0..1) to bring its zone to
    the setpoint (cool setpoint in cooling mode, heat setpoint in heating). This is an ideal
    proportional thermostat: a real ecobee cycles inside a ±0.5°F differential, which averages
    out over a slot. A NaN setpoint or ``off`` means no call.

    T0 (B, 3); t_out, sun (n, B); setpoint (n, B, 3). Steps whose t_out is NaN (padding of a
    short DST day in a batch) keep the state and run nothing. Returns T (n+1, B, 3) and the
    on-fractions (n, B, 3)."""
    n = t_out.shape[0]
    T = np.empty((n + 1,) + np.shape(T0))
    T[0] = T0
    on = np.zeros((n,) + np.shape(T0))
    q = model.q
    with np.errstate(invalid="ignore"):
        for k in range(n):
            pad = ~np.isfinite(t_out[k])
            to = np.where(pad, 0.0, t_out[k])
            sn = np.where(np.isfinite(sun[k]), sun[k], 0.0)
            free = T[k] + DT_H * model.free_rate(T[k], to, sn)
            sp = setpoint[k]
            need = (free - sp) if model.mode == "cool" else (sp - free)
            need = np.where(np.isfinite(need), need, 0.0)
            if off is not None:
                need = np.where(off[k], 0.0, need)
            frac = np.clip(need / (DT_H * np.where(q > 0, q, np.inf)), 0.0, 1.0)
            frac = np.where(pad[..., None], 0.0, frac)
            on[k] = frac
            nxt = free + model.sign * DT_H * q * frac
            T[k + 1] = np.where(pad[..., None], T[k], nxt)
    return T, on


# ---------------------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------------------


@dataclass
class Series:
    """15-minute house data on a contiguous UTC grid starting at ``start``. NaN = missing."""

    start: datetime
    tz: str
    temp: np.ndarray  # (n, 3) zone temperature (thermostat's averaged sensors)
    on_cool: np.ndarray  # (n, 3) stage-1 cooling on-fraction
    on_heat: np.ndarray  # (n, 3) heating on-fraction: max(compHeat1, auxHeat1) / slot
    cool_sp: np.ndarray  # (n, 3)
    heat_sp: np.ndarray  # (n, 3)
    hvac_off: np.ndarray  # (n, 3) bool, the unit reported hvac_mode 'off'
    t_out: np.ndarray  # (n,)
    sun: np.ndarray  # (n,) kW/m²
    cloud: np.ndarray  # (n,) %
    occ: np.ndarray  # (n, 3) 1 occupied, 0 empty, NaN unknown
    weather_sources: set[str] = field(default_factory=set)
    _local: list[datetime] | None = None

    @property
    def n(self) -> int:
        return int(self.t_out.shape[0])

    def ts(self, i: int) -> datetime:
        return self.start + i * SLOT

    @property
    def local(self) -> list[datetime]:
        if self._local is None:
            self._local = [to_local(self.ts(i), self.tz) for i in range(self.n)]
        return self._local

    def day_slices(self) -> dict[date, tuple[int, int]]:
        out: dict[date, tuple[int, int]] = {}
        for i, lt in enumerate(self.local):
            d = lt.date()
            if d in out:
                out[d] = (out[d][0], i + 1)
            else:
                out[d] = (i, i + 1)
        return out

    def index_of(self, ts: datetime) -> int:
        return int((ts - self.start).total_seconds() // SLOT_S)


def floor15(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    return ts - timedelta(minutes=ts.minute % 15, seconds=ts.second, microseconds=ts.microsecond)


_RUNTIME_SQL = text(
    """
    SELECT floor(extract(epoch FROM ts) / 900)::bigint AS b, unit_key,
           avg(zone_temp_f) AS temp,
           avg(comp_cool1)::float8 / 300.0 AS cool,
           avg(comp_heat1)::float8 / 300.0 AS heat1, avg(aux_heat1)::float8 / 300.0 AS aux1,
           avg(cool_sp_f) AS csp, avg(heat_sp_f) AS hsp, avg(outdoor_temp_f) AS tout,
           bool_or(hvac_mode = 'off') AS off
    FROM runtime_5m WHERE ts >= :s AND ts < :e
    GROUP BY 1, 2
    """
)
_OCC_SQL = text(
    """
    SELECT floor(extract(epoch FROM r.ts) / 900)::bigint AS b, s.unit_key,
           bool_or(r.occupied) AS any_occ, count(r.occupied) AS n
    FROM readings_5m r JOIN sensors s ON s.key = r.sensor_key
    WHERE r.ts >= :s AND r.ts < :e AND s.has_occupancy
    GROUP BY 1, 2
    """
)
_WEATHER_SQL = text(
    """
    SELECT ts, source, kind, temp_f, shortwave_wm2, cloud_cover
    FROM weather_hourly WHERE ts >= :s AND ts <= :e
    """
)
_SOURCE_RANK = {"open-meteo": 0, "simulator": 1}
_KIND_RANK = {"observed": 0, "forecast": 1}


def weather_inputs(
    session: Session, start: datetime, n: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, set[str]]:
    """Outdoor temperature, shortwave (kW/m²) and cloud cover at the midpoints of n 15-minute
    slots from ``start``, interpolated from weather_hourly (observed preferred over forecast,
    Open-Meteo over other sources). Shortwave is Open-Meteo's preceding-hour mean, so each
    hourly value is placed at the middle of its hour before interpolating. Slots more than
    90 minutes from any weather value are NaN."""
    nan = np.full(n, np.nan)
    if n <= 0:
        return nan, nan.copy(), nan.copy(), set()
    end = start + n * SLOT
    rows = session.execute(
        _WEATHER_SQL, {"s": start - timedelta(hours=3), "e": end + timedelta(hours=3)}
    ).all()
    best: dict[datetime, dict[str, tuple[tuple[int, int], float, str]]] = {}
    for ts, source, kind, temp, sw, cloud in rows:
        rank = (_KIND_RANK.get(kind, 2), _SOURCE_RANK.get(source, 2))
        slot = best.setdefault(ts, {})
        for name, val in (("temp", temp), ("sw", sw), ("cloud", cloud)):
            if val is None:
                continue
            cur = slot.get(name)
            if cur is None or rank < cur[0]:
                slot[name] = (rank, float(val), source)
    mids = np.array([(start + i * SLOT).timestamp() + SLOT_S / 2 for i in range(n)])
    sources: set[str] = set()

    def series(name: str, shift_s: float) -> np.ndarray:
        pts = sorted((ts.timestamp() + shift_s, v[1], v[2]) for ts, d in best.items() if (v := d.get(name)))
        if not pts:
            return np.full(n, np.nan)
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        sources.update(p[2] for p in pts)
        out = np.interp(mids, x, y)
        pos = np.searchsorted(x, mids)
        left = np.abs(mids - x[np.clip(pos - 1, 0, len(x) - 1)])
        right = np.abs(x[np.clip(pos, 0, len(x) - 1)] - mids)
        out[np.minimum(left, right) > 5400] = np.nan
        return out

    temp = series("temp", 0.0)
    sun = series("sw", -1800.0) / 1000.0
    cloud = series("cloud", 0.0)
    return temp, sun, cloud, sources


def load_series(session: Session, start: datetime, end: datetime, tz: str) -> Series:
    start, end = floor15(start), floor15(end)
    n = max(0, int((end - start).total_seconds() // SLOT_S))
    b0 = int(start.timestamp()) // SLOT_S

    def blank(shape: tuple[int, ...]) -> np.ndarray:
        return np.full(shape, np.nan)

    temp, on_c, on_h, csp, hsp, occ = (blank((n, 3)) for _ in range(6))
    off = np.zeros((n, 3), dtype=bool)
    tout_sum = np.zeros(n)
    tout_cnt = np.zeros(n)
    # Heating duty uses the unit's stage-1 heating metric like every other runtime figure
    # (analytics.daily.heat_metrics): comp_heat1 for a heat pump, aux_heat1 for a furnace.
    heat_is_comp = heat_metrics(session)
    for b, unit, t, c, h1, a1, cs, hs, to, is_off in session.execute(_RUNTIME_SQL, {"s": start, "e": end}):
        i, z = int(b) - b0, ZIDX.get(unit)
        if z is None or not 0 <= i < n:
            continue
        h = h1 if heat_is_comp.get(unit, False) else a1
        temp[i, z] = np.nan if t is None else t
        on_c[i, z] = np.nan if c is None else min(1.0, max(0.0, c))
        on_h[i, z] = np.nan if h is None else min(1.0, max(0.0, h))
        csp[i, z] = np.nan if cs is None else cs
        hsp[i, z] = np.nan if hs is None else hs
        off[i, z] = bool(is_off)
        if to is not None:
            tout_sum[i] += float(to)
            tout_cnt[i] += 1
    for b, unit, any_occ, cnt in session.execute(_OCC_SQL, {"s": start, "e": end}):
        i, z = int(b) - b0, ZIDX.get(unit)
        if z is None or not 0 <= i < n:
            continue
        occ[i, z] = 1.0 if any_occ else (0.0 if cnt else np.nan)
    w_temp, sun, cloud, sources = weather_inputs(session, start, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        t_out = np.where(tout_cnt > 0, tout_sum / np.maximum(tout_cnt, 1), w_temp)
    return Series(
        start=start,
        tz=tz,
        temp=temp,
        on_cool=on_c,
        on_heat=on_h,
        cool_sp=csp,
        heat_sp=hsp,
        hvac_off=off,
        t_out=t_out,
        sun=sun,
        cloud=cloud,
        occ=occ,
        weather_sources=sources,
    )


def fill_gaps(x: np.ndarray) -> np.ndarray:
    """Linear interpolation over NaNs (edges held flat); all-NaN stays NaN."""
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    if ok.all() or not ok.any():
        return x.copy()
    idx = np.arange(len(x))
    return np.interp(idx, idx[ok], x[ok])


def day_modes(s: Series) -> dict[date, str]:
    """Each local day's mode from total runtime (cooling vs heating). Days without any runtime
    take the nearest day's mode (they are free-floating data, valid for either set)."""
    raw: dict[date, str | None] = {}
    for d, (i0, i1) in s.day_slices().items():
        c = float(np.nansum(s.on_cool[i0:i1]))
        h = float(np.nansum(s.on_heat[i0:i1]))
        raw[d] = "cool" if c > h else ("heat" if h > 0 else None)
    days = sorted(raw)
    out: dict[date, str] = {}
    for j, d in enumerate(days):
        m = raw[d]
        if m is None:
            for dist in range(1, len(days)):
                for k in (j - dist, j + dist):
                    if 0 <= k < len(days) and raw[days[k]] is not None:
                        m = raw[days[k]]
                        break
                if m is not None:
                    break
        if m is not None:
            out[d] = m
    return out


def slot_modes(s: Series) -> np.ndarray:
    modes = day_modes(s)
    out = np.array([""] * s.n, dtype=object)
    for d, (i0, i1) in s.day_slices().items():
        out[i0:i1] = modes.get(d, "")
    return out


# ---------------------------------------------------------------------------------------
# fitting
# ---------------------------------------------------------------------------------------


@dataclass
class ZoneFit:
    theta: np.ndarray
    rel_hw90: np.ndarray  # relative 90% half-width (inf when unidentified)
    resid_std: float
    n: int


def _fit_zone(X: np.ndarray, dT: np.ndarray) -> ZoneFit:
    """Least squares of dT ≈ dt·X·θ with θ = exp(φ) > 0."""
    A = DT_H * X
    theta0, _ = nnls(A, dT, maxiter=50 * A.shape[1])
    phi0 = np.log(np.clip(theta0, _PARAM_FLOOR, math.exp(_LOG_HI) / 2))

    def resid(phi: np.ndarray) -> np.ndarray:
        return A @ np.exp(phi) - dT

    def jac(phi: np.ndarray) -> np.ndarray:
        return A * np.exp(phi)[None, :]

    sol = least_squares(
        resid,
        phi0,
        jac=jac,
        bounds=(_LOG_LO, _LOG_HI),
        method="trf",
        x_scale="jac",
        xtol=1e-12,
        ftol=1e-12,
        gtol=1e-12,
        max_nfev=500,
    )
    theta = np.exp(sol.x)
    r = A @ theta - dT
    n, p = A.shape
    s2 = float(r @ r) / max(1, n - p)
    # One-step residuals are serially correlated (sensor averaging, unmodeled slow loads), which
    # makes the textbook standard errors too small; widen by the AR(1) long-run variance factor.
    rho = float(np.corrcoef(r[:-1], r[1:])[0, 1]) if n > 3 and np.std(r) > 0 else 0.0
    rho = min(0.95, max(0.0, rho if math.isfinite(rho) else 0.0))
    s2 *= (1.0 + rho) / (1.0 - rho)
    # Standard errors from the Jacobian in θ (= A): cov = s² (AᵀA)⁻¹, via SVD so directions the
    # data cannot see (a regressor with no excitation, two collinear regressors) give infinity.
    _, sv, vt = np.linalg.svd(A, full_matrices=False)
    tol = sv.max() * 1e-9 if sv.size else 0.0
    good = sv > tol
    var = (vt[good].T ** 2) @ (s2 / sv[good] ** 2)
    blind = np.zeros(p, dtype=bool)
    for j in np.where(~good)[0]:
        blind |= np.abs(vt[j]) > 1e-6
    se = np.sqrt(var)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(blind, np.inf, Z90 * se / theta)
    return ZoneFit(theta=theta, rel_hw90=rel, resid_std=math.sqrt(float(r @ r) / max(1, n - p)), n=n)


def _window_starts(ok_inputs: np.ndarray, temp_ok: np.ndarray, lo: int, hi: int, horizon: int) -> np.ndarray:
    """Start indices k in [lo, hi - horizon] whose inputs are usable for k..k+H-1 and whose
    temperatures are known at k and k+H."""
    cs = np.concatenate([[0], np.cumsum(ok_inputs.astype(int))])
    ks = np.arange(lo, max(lo, hi - horizon))
    if ks.size == 0:
        return ks
    good = (cs[ks + horizon] - cs[ks] == horizon) & temp_ok[ks] & temp_ok[ks + horizon]
    return ks[good]


def _horizon_rmse(
    model: RCModel, s: Series, on: np.ndarray, starts: np.ndarray, horizon: int
) -> tuple[float, float, np.ndarray, float]:
    """(model RMSE, persistence RMSE, per-zone model RMSE, model bias) H steps ahead."""
    idx = starts[None, :] + np.arange(horizon)[:, None]  # (H, B)
    pred = simulate_measured(model, s.temp[starts], s.t_out[idx], np.nan_to_num(s.sun[idx]), on[idx])
    truth = s.temp[starts + horizon]
    err = pred - truth
    pers = s.temp[starts] - truth
    return (
        float(np.sqrt(np.mean(err**2))),
        float(np.sqrt(np.mean(pers**2))),
        np.sqrt(np.mean(err**2, axis=0)),
        float(np.mean(err)),
    )


def runtime_backcast(
    model: RCModel, s: Series, days: list[tuple[int, int]], weights: np.ndarray
) -> list[tuple[float, float]]:
    """(predicted, actual) weighted stage-1 seconds per day when each held-out day is replayed
    from its measured midnight temperatures through the thermostat emulation with the
    measured setpoints, outdoor temperature and sun."""
    on_meas = s.on_cool if model.mode == "cool" else s.on_heat
    sp_all = s.cool_sp if model.mode == "cool" else s.heat_sp
    out: list[tuple[float, float]] = []
    for i0, i1 in days:
        T0 = s.temp[i0]
        if not np.isfinite(T0).all():
            continue
        sp = np.column_stack([fill_gaps(sp_all[i0:i1, z]) for z in range(3)])
        t_out = fill_gaps(s.t_out[i0:i1])
        sun = np.nan_to_num(s.sun[i0:i1])
        if not np.isfinite(t_out).all() or not np.isfinite(sp).all():
            continue
        _, on = emulate(
            model, T0[None, :], t_out[:, None], sun[:, None], sp[:, None, :], s.hvac_off[i0:i1][:, None, :]
        )
        pred = float((on[:, 0, :] * SLOT_S).sum(axis=0) @ weights)
        actual = float((np.nan_to_num(on_meas[i0:i1]) * SLOT_S).sum(axis=0) @ weights)
        if actual >= 1800.0:  # at least 30 weighted minutes, or the ratio means nothing
            out.append((pred, actual))
    return out


@dataclass
class ModeFit:
    model: RCModel
    zone_fits: dict[str, ZoneFit]
    metrics: dict[str, Any]
    status: str
    notes: str
    train_start: date
    train_end: date


def fit_mode(s: Series, mode: str, k_split: int, weights: np.ndarray) -> ModeFit | None:
    """Fit one mode's parameter set on slots before ``k_split`` and score it after."""
    t0 = time.monotonic()
    modes = slot_modes(s)
    on = s.on_cool if mode == "cool" else s.on_heat
    opp = s.on_heat if mode == "cool" else s.on_cool
    sign = -1.0 if mode == "cool" else 1.0
    temp_ok = np.isfinite(s.temp).all(axis=1)
    inputs_ok = (
        (modes == mode)
        & np.isfinite(on).all(axis=1)
        & np.isfinite(s.t_out)
        & ~(np.nan_to_num(opp) > 0).any(axis=1)
    )
    sun_known = np.isfinite(s.sun)
    if sun_known.any():
        inputs_ok &= sun_known  # a gap in the weather is a gap, not a cloudy hour
    # With no shortwave at all the sun terms see zeros and are reported as not pinned down.
    sun = np.nan_to_num(s.sun)
    ks = np.arange(s.n - 1)
    step_ok = inputs_ok[:-1] & temp_ok[:-1] & temp_ok[1:] & (ks + 1 < k_split)
    train = ks[step_ok]
    if train.size < MIN_TRAIN_STEPS:
        return None
    starts_1h = _window_starts(inputs_ok, temp_ok, k_split, s.n, SLOTS_PER_HOUR)
    if starts_1h.size < MIN_TEST_STARTS:
        return None
    T, Tn = s.temp[train], s.temp[train + 1]
    zone_fits: dict[str, ZoneFit] = {}
    for z in ZONES:
        X = regressors(z, T, s.t_out[train], sun[train], on[train], sign)
        zone_fits[z] = _fit_zone(X, Tn[:, ZIDX[z]] - T[:, ZIDX[z]])
    model = RCModel(
        mode=mode, main=zone_fits["main"].theta, up=zone_fits["up"].theta, bed=zone_fits["bed"].theta
    )

    rmse_1h, pers_1h, by_zone_1h, bias_1h = _horizon_rmse(model, s, on, starts_1h, SLOTS_PER_HOUR)
    starts_24h = _window_starts(inputs_ok, temp_ok, k_split, s.n, SLOTS_PER_DAY)
    if starts_24h.size:
        rmse_24h, pers_24h, _, bias_24h = _horizon_rmse(model, s, on, starts_24h, SLOTS_PER_DAY)
    else:
        rmse_24h = pers_24h = bias_24h = None

    test_days = []
    for i0, i1 in s.day_slices().values():
        if i0 < k_split or i1 - i0 < 90:
            continue
        span = slice(i0, i1)
        if (modes[span] == mode).all() and (inputs_ok[span] & temp_ok[span]).mean() >= 0.9:
            test_days.append((i0, i1))
    pairs = runtime_backcast(model, s, test_days, weights)
    rel = np.array([(p - a) / a for p, a in pairs]) if pairs else np.array([])

    finite = all(np.isfinite(zf.theta).all() for zf in zone_fits.values()) and math.isfinite(rmse_1h)
    beats = rmse_1h < pers_1h
    status = (
        "failed" if not finite else ("active" if rmse_1h <= ACTIVE_MAX_RMSE_1H_F and beats else "candidate")
    )

    unpinned = [
        f"{z}.{n}"
        for z in ZONES
        for n, r in zip(PARAM_NAMES[z], zone_fits[z].rel_hw90, strict=True)
        if not (r <= REL_HALF_WIDTH_LIMIT)
    ]
    lt = s.local
    train_start = lt[int(train[0])].date()
    train_end = lt[int(train[-1])].date()

    def r2(x: float | None) -> float | None:
        return None if x is None else round(float(x), 3)

    metrics: dict[str, Any] = {
        "rmse_1h": r2(rmse_1h),
        "rmse_24h": r2(rmse_24h),
        "persistence_rmse_1h": r2(pers_1h),
        "persistence_rmse_24h": r2(pers_24h),
        "bias_1h": r2(bias_1h),
        "bias_24h": r2(bias_24h),
        "rmse_1h_by_zone": {z: r2(v) for z, v in zip(ZONES, by_zone_1h, strict=True)},
        "n_points": int(train.size),
        "n_test_1h": int(starts_1h.size),
        "n_test_24h": int(starts_24h.size),
        "runtime_days": int(rel.size),
        "runtime_err_rmse": r2(float(np.sqrt(np.mean(rel**2)))) if rel.size else None,
        "runtime_err_bias": r2(float(np.mean(rel))) if rel.size else None,
        "runtime_err_scatter": r2(float(np.std(rel, ddof=1))) if rel.size > 1 else None,
        "fit_seconds": round(time.monotonic() - t0, 2),
    }
    label = "Cooling" if mode == "cool" else "Heating"
    parts = [
        (
            f"{label} RC fit on {train_start.isoformat()} to {train_end.isoformat()} "
            f"({train.size} one-step pairs), scored on the following week."
        )
    ]
    parts.append(
        f"1-hour-ahead RMSE {rmse_1h:.2f}°F vs persistence {pers_1h:.2f}°F"
        + (f"; 24-hour-ahead {rmse_24h:.2f}°F vs {pers_24h:.2f}°F." if rmse_24h is not None else ".")
    )
    if rel.size:
        parts.append(
            f"Replaying {rel.size} held-out day(s) with the measured setpoints, daily runtime is off by "
            f"{metrics['runtime_err_rmse'] * 100:.0f}% (bias {metrics['runtime_err_bias'] * 100:+.0f}%)."
        )
    if unpinned:
        parts.append(
            "Not pinned down by the data (90% interval wider than ±50%): "
            + "; ".join(f"{p} ({PARAM_HELP[p]})" for p in unpinned)
            + "."
        )
    else:
        parts.append("Every parameter is pinned down (90% interval within ±50%).")
    if status == "active":
        parts.append(
            "Passes its checks (1-hour RMSE ≤ 1.0°F and better than persistence); still shadow-only "
            "until it beats the linked-floors rule in backtests."
        )
    elif status == "candidate":
        why = []
        if rmse_1h > ACTIVE_MAX_RMSE_1H_F:
            why.append(f"1-hour RMSE above {ACTIVE_MAX_RMSE_1H_F:.1f}°F")
        if not beats:
            why.append("no better than persistence")
        parts.append("Kept as a candidate: " + " and ".join(why) + ".")
    else:
        parts.append("Failed: the fit produced non-finite values.")
    return ModeFit(
        model=model,
        zone_fits=zone_fits,
        metrics=metrics,
        status=status,
        notes=" ".join(parts),
        train_start=train_start,
        train_end=train_end,
    )


def _tz(session: Session) -> str:
    return get_setting(session, "location", LocationSettings).tz


def _weights(session: Session) -> np.ndarray:
    w = {
        u.key: float(u.power_weight if u.power_weight is not None else 1.0)
        for u in session.scalars(select(Unit)).all()
    }
    return np.array([w.get(z, 1.0) for z in ZONES])


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, np.integer):
        return int(x)
    return x


def fit_rc(session: Session, now: datetime, days: int = 28) -> list[int]:
    """Fit C, R_out, R_mu, R_mb, k_s, a_sun, Q per zone (main, up, bed) by least squares on
    15-minute data (zone temps = thermostat averaged temps, on fraction = stage-1 runtime / slot,
    T_out and shortwave from weather). Walk-forward: train on all but the last 7 days, score
    1 h and 24 h ahead RMSE on the held-out week. Report parameters the data cannot pin down
    (relative CI half-width > 50%). Store model_fits kind='rc' as 'candidate' (status
    'active' only if 1 h RMSE <= 1.0°F and it beats persistence). Returns ids.

    A mode is skipped (no row) when it has under three days of training data or too little
    held-out data to score. A new active fit retires the mode's previous active fit; a new
    fit of any status retires older candidates (a failed or candidate fit never displaces the
    last good one)."""
    tz = _tz(session)
    end = floor15(now)
    start = end - timedelta(days=days)
    split = end - timedelta(days=TEST_DAYS)
    if split <= start:
        return []
    s = load_series(session, start, end, tz)
    k_split = s.index_of(split)
    weights = _weights(session)
    ids: list[int] = []
    for mode in ("cool", "heat"):
        mf = fit_mode(s, mode, k_split, weights)
        if mf is None:
            continue
        params = {
            "mode": mode,
            "form": "rate",
            "dt_h": DT_H,
            "zones": mf.model.to_params(),
            "rel_hw90": {
                z: {n: float(r) for n, r in zip(PARAM_NAMES[z], mf.zone_fits[z].rel_hw90, strict=True)}
                for z in ZONES
            },
            "resid_std_f": {z: mf.zone_fits[z].resid_std for z in ZONES},
            "unidentified": [
                f"{z}.{n}"
                for z in ZONES
                for n, r in zip(PARAM_NAMES[z], mf.zone_fits[z].rel_hw90, strict=True)
                if not (r <= REL_HALF_WIDTH_LIMIT)
            ],
            "physical": mf.model.physical(),
            "weather_sources": sorted(s.weather_sources),
        }
        if mf.status == "active":
            session.execute(
                update(ModelFit)
                .where(ModelFit.kind == "rc", ModelFit.mode == mode, ModelFit.status == "active")
                .values(status="retired")
            )
        session.execute(
            update(ModelFit)
            .where(ModelFit.kind == "rc", ModelFit.mode == mode, ModelFit.status == "candidate")
            .values(status="retired")
        )
        row = ModelFit(
            kind="rc",
            unit_key=None,
            mode=mode,
            train_start=mf.train_start,
            train_end=mf.train_end,
            params=_jsonable(params),
            metrics=_jsonable(mf.metrics),
            status=mf.status,
            notes=mf.notes,
        )
        session.add(row)
        session.flush()
        ids.append(int(row.id))
    return ids


def active_rc(session: Session) -> dict[str, tuple[ModelFit, RCModel]]:
    """mode -> (row, model) for the newest active RC fit of each mode."""
    out: dict[str, tuple[ModelFit, RCModel]] = {}
    rows = session.scalars(
        select(ModelFit)
        .where(ModelFit.kind == "rc", ModelFit.status == "active")
        .order_by(ModelFit.created_at.desc(), ModelFit.id.desc())
    ).all()
    for row in rows:
        mode = row.mode or (row.params or {}).get("mode")
        if mode in ("cool", "heat") and mode not in out:
            params = dict(row.params or {})
            params["mode"] = mode
            out[mode] = (row, RCModel.from_params(params))
    return out


# ---------------------------------------------------------------------------------------
# room offsets
# ---------------------------------------------------------------------------------------

_OFFSET_SQL = text(
    """
    SELECT r.ts, s.room_key, avg(r.temp_f) AS room_t, rt.zone_temp_f, rt.comp_cool1,
           greatest(rt.comp_heat1, rt.aux_heat1) AS heat, rt.hvac_mode
    FROM readings_5m r
    JOIN sensors s ON s.key = r.sensor_key AND s.is_active
    JOIN rooms rm ON rm.key = s.room_key AND rm.has_sensor
    JOIN runtime_5m rt ON rt.ts = r.ts AND rt.unit_key = s.unit_key
    WHERE r.ts >= :s AND r.ts < :e AND r.temp_f IS NOT NULL AND rt.zone_temp_f IS NOT NULL
    GROUP BY r.ts, s.room_key, rt.zone_temp_f, rt.comp_cool1, rt.comp_heat1, rt.aux_heat1, rt.hvac_mode
    """
)


def _period(local_hour: int) -> str:
    return "night" if local_hour >= NIGHT_START_H or local_hour < NIGHT_END_H else "day"


def fit_room_offsets(session: Session, now: datetime, days: int = 14) -> int | None:
    """Per sensored room: median (room temp - its unit's zone temp) by period (day/night) and mode.
    Store model_fits kind='room_offsets' active. Returns id.

    params = {"offsets": {room_key: {"day": f, "night": f}}, "n": {room_key: {"day": n, "night": n}},
    "by_mode": {room_key: {"cool"|"heat": {"day": f, "night": f}}}}; night is 21:00-07:00 local.
    A period with under an hour of readings is null (never a guess). Returns None (and stores
    nothing) when no sensored room has any reading paired with its unit's zone temperature."""
    tz = _tz(session)
    end = now
    start = end - timedelta(days=days)
    sensored = {r.key for r in ROOMS if r.has_sensor}
    samples: dict[str, dict[str, list[float]]] = {}
    by_mode: dict[str, dict[str, dict[str, list[float]]]] = {}
    for ts, room, room_t, zone_t, cool, heat, hvac in session.execute(_OFFSET_SQL, {"s": start, "e": end}):
        if room not in sensored or room_t is None or zone_t is None:
            continue
        period = _period(to_local(ts, tz).hour)
        off = float(room_t) - float(zone_t)
        samples.setdefault(room, {"day": [], "night": []})[period].append(off)
        mode = (
            "cool"
            if (cool or 0) > 0 or hvac == "cool"
            else "heat"
            if (heat or 0) > 0 or hvac in ("heat", "auxHeatOnly")
            else None
        )
        if mode:
            by_mode.setdefault(room, {}).setdefault(mode, {"day": [], "night": []})[period].append(off)
    if not samples:
        return None

    def med(xs: list[float]) -> float | None:
        return round(float(np.median(xs)), 2) if len(xs) >= MIN_OFFSET_POINTS else None

    offsets = {room: {p: med(v) for p, v in per.items()} for room, per in samples.items()}
    counts = {room: {p: len(v) for p, v in per.items()} for room, per in samples.items()}
    mode_offsets = {
        room: {m: {p: med(v) for p, v in per.items()} for m, per in modes.items()}
        for room, modes in by_mode.items()
    }
    spread = {
        room: {
            p: (round(float(np.median(np.abs(np.array(v) - np.median(v)))), 2) if v else None)
            for p, v in per.items()
        }
        for room, per in samples.items()
    }
    session.execute(
        update(ModelFit)
        .where(ModelFit.kind == "room_offsets", ModelFit.status == "active")
        .values(status="retired")
    )
    row = ModelFit(
        kind="room_offsets",
        unit_key=None,
        mode=None,
        train_start=local_date(start, tz),
        train_end=local_date(end, tz),
        params={"offsets": offsets, "n": counts, "by_mode": mode_offsets, "night": "21:00-07:00"},
        metrics={"n_points": int(sum(sum(c.values()) for c in counts.values())), "mad_f": spread},
        status="active",
        notes=(
            f"Median room minus thermostat-average temperature over {days} days, split day / night "
            "(21:00-07:00) and by mode; null where a room has under an hour of readings. Rooms without "
            "a sensor are never estimated."
        ),
    )
    session.add(row)
    session.flush()
    return int(row.id)

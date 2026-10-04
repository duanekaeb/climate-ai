"""Comfort, duty, maxed-out minutes and drift."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from climate.analytics.baseline import (
    MODES,
    BaselineFit,
    active_fits,
    expected_seconds,
    involved_modes,
    pre_period_fits,
)
from climate.analytics.daily import (
    MAXED_DUTY,
    daily_rows,
    full_day_seconds,
    heat_metrics,
    hourly_runtime,
    house_tz,
    is_complete,
    unit_order,
)
from climate.api.schemas import ComfortRow, DriftReport, DriftUnit
from climate.house import ROOMS, UNIT_KEYS
from climate.store.app_settings import (
    DEFAULT_COMFORT,
    ControlSettings,
    OccupancySettings,
    UnitComfort,
    get_setting,
)
from climate.timeutil import day_bounds_utc, floor_slot, in_window, local_date, utcnow

COMFORT_TOL_F = 0.5
STATE_MAX_AGE_S = 15 * 60  # a room_states row describes the room for at most this long
DRIFT_Z = 2.0
MIN_DRIFT_DAYS = 3
_SLOT = 300.0
UNIT_NAME = {"main": "Main floor", "up": "Upstairs", "bed": "Bed/office wing"}
MODE_WORD = {"cool": "cooling", "heat": "heating"}


# ---------------------------------------------------------------------------------------
# today's runtime, duty and maxed-out minutes (Live tab)
# ---------------------------------------------------------------------------------------

_LAST_HOUR_SQL = text(
    """
    SELECT unit_key,
           sum(comp_cool1)::float8, sum(comp_heat1)::float8, sum(aux_heat1)::float8,
           sum(LEAST(300.0, extract(epoch FROM (CAST(:now AS timestamptz) - ts))))::float8 AS elapsed
    FROM runtime_5m
    WHERE ts >= :h0 AND ts < :now
    GROUP BY unit_key
    """
)


def unit_today(session: Session, now: datetime, tz: str) -> dict[str, dict[str, float | None]]:
    """unit_key -> {'today_runtime_min', 'duty_last_hour_pct', 'maxed_minutes_today'}.

    Stage-1 runtime (cooling + the unit's heating metric) from local midnight to ``now``.
    Duty over the last hour = runtime / the time the slots with data cover (None without data).
    Maxed = minutes in local hours whose duty (over the covered slots) is >= 95%."""
    start = day_bounds_utc(local_date(now, tz), tz)[0]
    comp = heat_metrics(session)
    units = list(dict.fromkeys(list(comp) or UNIT_KEYS))
    out: dict[str, dict[str, float | None]] = {
        u: {"today_runtime_min": 0.0, "duty_last_hour_pct": None, "maxed_minutes_today": 0.0} for u in units
    }
    hr = hourly_runtime(session, start, now, tz, heat_is_comp=comp)
    if hr.hour.size:
        covered = hr.slots * _SLOT
        duty = np.divide(hr.stage1, covered, out=np.zeros_like(covered), where=covered > 0)
        maxed = np.where(duty >= MAXED_DUTY, hr.slots * 5.0, 0.0)
        for u in np.unique(hr.unit):
            m = hr.unit == u
            d = out.setdefault(str(u), {"today_runtime_min": 0.0, "duty_last_hour_pct": None, "maxed_minutes_today": 0.0})
            d["today_runtime_min"] = round(float(hr.stage1[m].sum()) / 60.0, 1)
            d["maxed_minutes_today"] = float(maxed[m].sum())
    for u, cool, heat1, aux1, elapsed in session.execute(_LAST_HOUR_SQL, {"now": now, "h0": now - timedelta(hours=1)}):
        if not elapsed or elapsed <= 0:
            continue
        run = (cool or 0.0) + ((heat1 or 0.0) if comp.get(u, False) else (aux1 or 0.0))
        d = out.setdefault(u, {"today_runtime_min": 0.0, "duty_last_hour_pct": None, "maxed_minutes_today": 0.0})
        d["duty_last_hour_pct"] = round(min(100.0, run / elapsed * 100.0), 1)
    return out


# ---------------------------------------------------------------------------------------
# comfort
# ---------------------------------------------------------------------------------------

_READINGS_SQL = text(
    """
    SELECT s.room_key,
           extract(epoch FROM r.ts)::float8 AS t,
           avg(r.temp_f)::float8 AS temp,
           bool_or(r.occupied) FILTER (WHERE s.has_occupancy) AS any_occ,
           count(r.occupied) FILTER (WHERE s.has_occupancy)::int AS n_occ
    FROM readings_5m r
    JOIN sensors s ON s.key = r.sensor_key
    WHERE r.ts >= :t0 AND r.ts < :t1 AND s.is_active
    GROUP BY s.room_key, r.ts
    """
)
_STATES_SQL = text(
    """
    SELECT room_key, extract(epoch FROM ts)::float8, state
    FROM room_states
    WHERE ts >= :t0 AND ts < :t1
    ORDER BY room_key, ts
    """
)


def _window_mask(local: list[datetime], windows: list[Any]) -> np.ndarray:
    return np.array([any(in_window(t, w.start, w.end, w.days) for w in windows) for t in local], dtype=bool)


def comfort_period(session: Session, t0: datetime, t1: datetime, tz: str) -> list[dict[str, Any]]:
    """Per sensored room with a comfort target, over [t0, t1):

    - each 5-minute slot's state is the latest room_states row (at most 15 minutes old) at
      the slot's midpoint; without one it falls back to the room's own signals: inside its
      sleep window -> asleep, any occupancy sensor True -> occupied, all False -> empty,
      nothing known -> occupied (uncertain means occupied);
    - occupied/asleep slots with a temperature (mean of the room's sensors in readings_5m)
      are scored against the unit's comfort band: the night band inside any sleep window of
      the unit's sleep rooms, else the day band, with 0.5°F tolerance;
    - worst excursion = the largest distance past the band edge (0 when never outside).
    Returns dicts with room_key, occupied_min, scored_min, in_band_pct, miss_min,
    worst_excursion_f."""
    control = get_setting(session, "control", ControlSettings)
    occ = get_setting(session, "occupancy", OccupancySettings)
    z = ZoneInfo(tz)
    g0 = floor_slot(t0.astimezone(UTC)).timestamp()
    n = max(int(math.ceil((t1.timestamp() - g0) / _SLOT)), 0)
    grid = g0 + _SLOT * np.arange(n)
    mids = [datetime.fromtimestamp(g + _SLOT / 2, z) for g in grid]

    temp: dict[str, np.ndarray] = defaultdict(lambda: np.full(n, np.nan))
    any_occ: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(n, dtype=bool))
    n_occ: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(n, dtype=np.int64))
    seen: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(n, dtype=bool))
    for room, t, tf, ao, no in session.execute(_READINGS_SQL, {"t0": datetime.fromtimestamp(g0, UTC), "t1": t1}):
        i = int(round((t - g0) / _SLOT))
        if 0 <= i < n:
            seen[room][i] = True
            if tf is not None:
                temp[room][i] = tf
            any_occ[room][i] = bool(ao)
            n_occ[room][i] = no or 0

    states: dict[str, tuple[list[float], list[str]]] = defaultdict(lambda: ([], []))
    t_state0 = datetime.fromtimestamp(g0 - STATE_MAX_AGE_S, UTC)
    for room, t, st in session.execute(_STATES_SQL, {"t0": t_state0, "t1": t1}):
        states[room][0].append(t)
        states[room][1].append(st)

    night: dict[str, np.ndarray] = {}
    for u in UNIT_KEYS:
        wins = [w for r in ROOMS if r.unit_key == u for w in occ.sleep_windows.get(r.key, [])]
        night[u] = _window_mask(mids, wins) if wins else np.zeros(n, dtype=bool)

    out: list[dict[str, Any]] = []
    for room in ROOMS:
        if not room.has_sensor or not room.has_comfort_target:
            continue
        rk = room.key
        state = np.full(n, "", dtype=object)
        ts, sv = states.get(rk, ([], []))
        if ts:
            ta = np.array(ts)
            idx = np.searchsorted(ta, grid + _SLOT / 2, side="right") - 1
            ok = (idx >= 0) & ((grid + _SLOT / 2) - ta[np.clip(idx, 0, None)] <= STATE_MAX_AGE_S)
            state[ok] = np.array(sv, dtype=object)[idx[ok]]
        fb = (state == "") & seen[rk]
        if fb.any():
            asleep = _window_mask(mids, occ.sleep_windows.get(rk, [])) if room.is_sleep_room else np.zeros(n, dtype=bool)
            fallback = np.where(asleep, "asleep",
                                np.where(any_occ[rk], "occupied",
                                         np.where(n_occ[rk] > 0, "empty", "occupied")))
            state[fb] = fallback[fb]
        occupied = (state == "occupied") | (state == "asleep")
        band: UnitComfort = control.comfort.get(room.unit_key) or DEFAULT_COMFORT[room.unit_key]
        lo = np.where(night[room.unit_key], band.night.heat_f, band.day.heat_f)
        hi = np.where(night[room.unit_key], band.night.cool_f, band.day.cool_f)
        tr = temp[rk]
        scored = occupied & np.isfinite(tr)
        inside = scored & (tr >= lo - COMFORT_TOL_F) & (tr <= hi + COMFORT_TOL_F)
        exc = np.maximum(np.maximum(lo - tr, tr - hi), 0.0)
        n_sc = int(scored.sum())
        out.append({
            "room_key": rk,
            "unit_key": room.unit_key,
            "occupied_min": float(occupied.sum()) * 5.0,
            "scored_min": n_sc * 5.0,
            "in_band_pct": round(float(inside.sum()) / n_sc * 100.0, 1) if n_sc else None,
            "miss_min": float(n_sc - int(inside.sum())) * 5.0,
            "worst_excursion_f": round(float(exc[scored].max()), 1) if n_sc else None,
        })
    return out


def comfort(session: Session, days: int = 7) -> list[ComfortRow]:
    """Per sensored room: minutes occupied/asleep (room_states) and the share of those
    minutes inside the unit's comfort band for the period (readings_5m temp vs band)."""
    tz = house_tz(session)
    now = utcnow()
    rows = comfort_period(session, now - timedelta(days=days), now, tz)
    return [
        ComfortRow(room_key=r["room_key"], occupied_min=r["occupied_min"], in_band_pct=r["in_band_pct"],
                   worst_excursion_f=r["worst_excursion_f"])
        for r in rows
    ]


# ---------------------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------------------


def drift(session: Session, recent_days: int = 7) -> DriftReport:
    """Recent mean residual of each active baseline vs its training residual spread; z > 2
    for the recent window -> drifting (triggers a Claude investigation via the worker).

    The recent window is the last ``recent_days`` finished local days. Residuals are taken
    against the baseline trained on the 90 days BEFORE that window (an active fit refitted
    nightly has already learned the recent days and would never drift); if no such fit
    exists the active fit is used and the note says so. z = mean residual / (residual std *
    sqrt((1 + rho) / ((1 - rho) * m))) with rho the training lag-1 autocorrelation in [0, 0.9];
    |z| > 2 is drifting. Only unit/modes in use during the window are checked."""
    tz = house_tz(session)
    end = local_date(utcnow(), tz) - timedelta(days=1)
    start = end - timedelta(days=recent_days - 1)
    active = active_fits(session)
    if not active:
        return DriftReport(units=[], note="No active baselines yet, so there is nothing to check for drift.")
    pre = pre_period_fits(session, start, tz)
    rows = daily_rows(session, start, end, tz)
    fits: dict[tuple[str, str], BaselineFit] = {k: pre.get(k) or v for k, v in active.items()}
    in_sample = sorted(k for k in active if k not in pre)
    in_use = involved_modes(rows, fits)

    units: list[DriftUnit] = []
    short: list[str] = []
    for key in sorted(fits, key=lambda k: (unit_order(k[0]), MODES.index(k[1]) if k[1] in MODES else 9)):
        if key not in in_use:
            continue
        unit, mode = key
        fit = fits[key]
        resid, exp = [], []
        for r in rows:
            if r.unit_key != unit or not is_complete(r):
                continue
            e = expected_seconds(fit, r)
            if math.isnan(e):
                continue
            resid.append(full_day_seconds(r, mode) - e)
            exp.append(e)
        m = len(resid)
        if m < MIN_DRIFT_DAYS:
            short.append(f"{UNIT_NAME.get(unit, unit).lower()} {MODE_WORD.get(mode, mode)}")
            continue
        rho = min(max(fit.resid_lag1, 0.0), 0.9)
        se = fit.resid_std_s * math.sqrt((1.0 + rho) / ((1.0 - rho) * m))
        mean_r = float(np.mean(resid))
        z = mean_r / se if se > 0 else 0.0
        mean_e = float(np.mean(exp))
        units.append(DriftUnit(
            unit_key=unit, mode=mode,  # type: ignore[arg-type]
            recent_days=m, resid_mean_pct=round(mean_r / mean_e * 100.0, 1) if mean_e > 0 else 0.0,
            z=round(z, 2), drifting=abs(z) > DRIFT_Z,
        ))

    drifting = [u for u in units if u.drifting]
    if drifting:
        parts = [f"{UNIT_NAME.get(u.unit_key, u.unit_key)} {MODE_WORD.get(u.mode, u.mode)} {u.resid_mean_pct:+.0f}% "
                 f"(z {u.z:+.1f})" for u in drifting]
        note = "Drifting from the weather baseline: " + "; ".join(parts) + ". Worth an investigation."
    elif units:
        note = f"No drift: every checked baseline is within {DRIFT_Z:.0f} standard errors over the last {recent_days} days."
    else:
        note = f"Nothing to check: no unit/mode in use has {MIN_DRIFT_DAYS}+ complete days in the last {recent_days} days."
    if short:
        note += " Too few complete recent days for: " + ", ".join(short) + "."
    if in_sample:
        note += (" No baseline from before the window for " + ", ".join(f"{u} {m}" for u, m in in_sample)
                 + "; the active fit was used, which has already seen these days.")
    return DriftReport(units=units, note=note)


def actions_by_status(session: Session, t0: datetime, t1: datetime) -> dict[str, int]:
    rows = session.execute(
        text("SELECT status, count(*) FROM control_actions WHERE ts >= :t0 AND ts < :t1 GROUP BY status ORDER BY status"),
        {"t0": t0, "t1": t1},
    )
    return {s: int(c) for s, c in rows}


def local_day_bounds(d: date, tz: str) -> tuple[datetime, datetime]:
    return day_bounds_utc(d, tz)

"""Daily aggregation of runtime_5m + weather into the rows every analysis uses.

Runtime metric = stage-1 seconds. Cooling: comp_cool1. Heating: comp_heat1 for a heat pump,
aux_heat1 for a furnace (units.equipment['heating'] in {'heat_pump', 'furnace'}). When the
equipment is unknown: comp_heat1 if the unit ever reports compressor heat, else aux_heat1.
Never add the *2 columns (stage-1 already includes stage-2 time).

Days are local calendar days in ``LocationSettings.tz``: a DST day has 276 or 300 five-minute
slots and 23 or 25 hourly outdoor temperatures. Per-day totals are NOT normalized here; each
row carries ``slots`` and ``expected_slots`` so callers can tell a partial day from a short
one. ``is_complete`` (>= 90% of the day's slots and of its outdoor hours) is the gate for
every fit; ``full_day_seconds`` is the one documented normalization (scale a >= 90% day up
to its full length, assuming the missing slots looked like the rest of the day).

Everything heavy is aggregated in SQL (GROUP BY hour) and shaped with numpy, so a year of
5-minute data for three units comes back in well under a second.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from climate.timeutil import day_bounds_utc

COMPLETE_FRACTION = 0.9  # share of a day's slots / outdoor hours needed for a "complete" day
MAXED_DUTY = 0.95  # an hour at or above this duty counts as maxed out
# weather_hourly observed sources in order of preference; runtime_5m.outdoor_temp_f is last.
WEATHER_SOURCES = ("open-meteo", "simulator", "nws", "ecobee")
RUNTIME_OUTDOOR = "runtime_5m"
MAX_INTERP_GAP_H = 3  # outdoor gaps up to this many hours are filled linearly
_HOUR = 3600.0


@dataclass
class DayRow:
    day: date  # local calendar day
    unit_key: str
    cool_s: float
    heat_s: float
    aux_s: float
    fan_s: float
    slots: int  # 5-minute slots with data (288 = complete; 276/300 on DST days)
    mode: str | None  # 'cool' | 'heat' | None (dominant equipment use)
    outdoor_mean_f: float | None
    outdoor_max_f: float | None
    hourly_outdoor_f: list[float] = field(default_factory=list)  # local hours with data
    cdd65: float | None = None
    hdd65: float | None = None
    maxed_min: float = 0.0  # minutes in local hours where duty >= 95%
    # Added by analytics (defaults keep older constructors working):
    expected_slots: int = 288  # slots in this local day (276 / 288 / 300)
    off_slots: int = 0  # slots with hvac_mode = 'off'
    weather_sources: list[str] = field(default_factory=list)  # sources behind hourly_outdoor_f

    # heat_s is the unit's heating metric (comp_heat1 for a heat pump, aux_heat1 for a
    # furnace). aux_s is BACKUP heat beyond that metric: aux_heat1 for a heat pump, 0 for a
    # furnace (whose aux_heat1 already is heat_s), so cool + heat + aux never double counts.

    @property
    def expected_hours(self) -> float:
        return self.expected_slots / 12.0

    @property
    def coverage(self) -> float:
        return self.slots / self.expected_slots if self.expected_slots else 0.0


# ---------------------------------------------------------------------------------------
# small helpers shared by the analytics modules
# ---------------------------------------------------------------------------------------


def unit_order(unit_key: str) -> tuple[int, str]:
    """Sort key: the house's unit order (main, up, bed), unknown units after, by name."""
    from climate.house import UNIT_KEYS

    return (UNIT_KEYS.index(unit_key), "") if unit_key in UNIT_KEYS else (len(UNIT_KEYS), unit_key)


def house_tz(session: Session) -> str:
    from climate.store.app_settings import LocationSettings, get_setting

    return get_setting(session, "location", LocationSettings).tz


def expected_slots(d: date, tz: str) -> int:
    start, end = day_bounds_utc(d, tz)
    return round((end - start).total_seconds() / 300.0)


def is_complete(row: DayRow) -> bool:
    """>= 90% of the day's 5-minute slots and >= 90% of its outdoor hours."""
    return (
        row.slots >= COMPLETE_FRACTION * row.expected_slots
        and len(row.hourly_outdoor_f) >= COMPLETE_FRACTION * row.expected_hours
    )


def mode_seconds(row: DayRow, mode: str) -> float:
    """The stage-1 runtime metric for one mode ('cool' -> cool_s, 'heat' -> heat_s)."""
    if mode == "cool":
        return row.cool_s
    if mode == "heat":
        return row.heat_s
    raise ValueError(f"mode must be 'cool' or 'heat', not {mode!r}")


def full_day_seconds(row: DayRow, mode: str) -> float:
    """Mode runtime scaled to the full local day (documented normalization for fits)."""
    if row.slots <= 0:
        return 0.0
    return mode_seconds(row, mode) * row.expected_slots / row.slots


def degree_hours(hourly_outdoor_f: list[float], balance_point_f: float, mode: str) -> float:
    """Degree-days from hourly data: sum(max(0, T-bp))/24 for cool, sum(max(0, bp-T))/24 for heat."""
    a = np.asarray(hourly_outdoor_f, dtype=float)
    if mode == "cool":
        d = a - balance_point_f
    elif mode == "heat":
        d = balance_point_f - a
    else:
        raise ValueError(f"mode must be 'cool' or 'heat', not {mode!r}")
    if a.size == 0:
        return 0.0
    return float(np.clip(d, 0.0, None).sum() / 24.0)


def day_degree_days(row: DayRow, balance_point_f: float, mode: str) -> float | None:
    """Degree-days for the FULL local day: degree_hours over the hours with data, scaled by
    expected hours / hours present (a no-op on complete days). None without outdoor data."""
    n = len(row.hourly_outdoor_f)
    if n == 0:
        return None
    return degree_hours(row.hourly_outdoor_f, balance_point_f, mode) * row.expected_hours / n


def degree_days_grid(rows: Sequence[DayRow], balance_points: np.ndarray, mode: str) -> np.ndarray:
    """Vectorized ``day_degree_days`` for many days x many balance points -> (n_days, n_bp).
    Rows without outdoor data give NaN."""
    if mode not in ("cool", "heat"):
        raise ValueError(f"mode must be 'cool' or 'heat', not {mode!r}")
    bps = np.asarray(balance_points, dtype=float)
    width = max((len(r.hourly_outdoor_f) for r in rows), default=0)
    if not rows or width == 0:
        return np.full((len(rows), bps.size), np.nan)
    temps = np.full((len(rows), width), np.nan)
    scale = np.full(len(rows), np.nan)
    for i, r in enumerate(rows):
        n = len(r.hourly_outdoor_f)
        if n:
            temps[i, :n] = r.hourly_outdoor_f
            scale[i] = r.expected_hours / n
    diff = temps[:, :, None] - bps[None, None, :] if mode == "cool" else bps[None, None, :] - temps[:, :, None]
    dd = np.nansum(np.clip(diff, 0.0, None), axis=1) / 24.0
    return dd * scale[:, None]


# ---------------------------------------------------------------------------------------
# equipment: which column is the heating metric
# ---------------------------------------------------------------------------------------

_HEAT_SQL = text(
    """
    SELECT u.key,
           u.equipment->>'heating' AS heating,
           CASE WHEN u.equipment->>'heating' IN ('heat_pump', 'furnace') THEN NULL
                ELSE EXISTS (SELECT 1 FROM runtime_5m r WHERE r.unit_key = u.key AND r.comp_heat1 > 0)
           END AS has_comp_heat
    FROM units u
    ORDER BY u.sort, u.key
    """
)


# The units rows as they stand (equipment and row version): a change to any unit, such as its
# equipment being set in setup, invalidates the cached answer at once.
_UNITS_VERSION_SQL = text("SELECT key, equipment->>'heating', xmin::text FROM units ORDER BY sort, key")
HEAT_METRICS_TTL_S = 600.0
_heat_cache: dict[str, tuple[tuple, float, dict[str, bool]]] = {}
_heat_lock = threading.Lock()


def heat_metrics(session: Session) -> dict[str, bool]:
    """unit_key -> True when the heating metric is comp_heat1 (heat pump), False when it is
    aux_heat1 (furnace). Unknown equipment: compressor heat if the unit ever reported it.

    Cached per process for ``HEAT_METRICS_TTL_S`` (10 minutes) per database, keyed on the units
    rows' equipment and row versions: only the "ever reported compressor heat" scan of
    runtime_5m (needed while a unit's equipment is unknown) is reused, so a unit that starts
    reporting compressor heat switches metric within 10 minutes."""
    units = tuple(tuple(r) for r in session.execute(_UNITS_VERSION_SQL))
    db = str(session.get_bind().url)
    now = time.monotonic()
    with _heat_lock:
        hit = _heat_cache.get(db)
    if hit is not None and hit[0] == units and now - hit[1] < HEAT_METRICS_TTL_S:
        return dict(hit[2])
    out: dict[str, bool] = {}
    for key, heating, has_comp in session.execute(_HEAT_SQL):
        if heating == "heat_pump":
            out[key] = True
        elif heating == "furnace":
            out[key] = False
        else:
            out[key] = bool(has_comp)
    with _heat_lock:
        _heat_cache[db] = (units, now, dict(out))
    return out


def clear_heat_metrics_cache() -> None:
    with _heat_lock:
        _heat_cache.clear()


def unit_weights(session: Session) -> dict[str, float]:
    return {k: float(w) for k, w in session.execute(text("SELECT key, power_weight FROM units ORDER BY sort, key"))}


# ---------------------------------------------------------------------------------------
# hourly aggregation (SQL) -> numpy
# ---------------------------------------------------------------------------------------


@dataclass
class HourlyRuntime:
    """Per (unit, UTC hour) sums from runtime_5m, as parallel numpy arrays. ``day`` is the
    local day of the hour's start (exact for whole-hour UTC offsets, DST included; in a
    half-hour-offset zone the half hour after local midnight counts to the day before)."""

    unit: np.ndarray  # object (unit_key)
    hour: np.ndarray  # float64 epoch seconds of the UTC hour start
    day: np.ndarray  # int64 date ordinals (local day)
    cool: np.ndarray
    heat: np.ndarray  # heating metric per unit (comp_heat1 or aux_heat1)
    aux: np.ndarray  # backup heat (aux_heat1 for heat pumps, 0 for furnaces)
    fan: np.ndarray
    slots: np.ndarray
    off_slots: np.ndarray
    outdoor: np.ndarray  # mean runtime_5m.outdoor_temp_f in the hour (NaN if none)
    zone: np.ndarray  # mean zone_temp_f in the hour (NaN if none)
    cool_mode_slots: np.ndarray  # slots whose hvac_mode allows cooling ('cool' / 'auto')
    known_mode_slots: np.ndarray  # slots with a non-null hvac_mode

    @property
    def stage1(self) -> np.ndarray:
        return self.cool + self.heat

    def for_unit(self, unit_key: str) -> np.ndarray:
        return self.unit == unit_key


_HOURLY_SQL = """
SELECT unit_key,
       extract(epoch FROM date_trunc('hour', ts, 'UTC'))::float8 AS hour_utc,
       sum(comp_cool1)::float8 AS cool1,
       sum(comp_heat1)::float8 AS heat1,
       sum(aux_heat1)::float8 AS aux1,
       sum(fan)::float8 AS fan,
       count(*)::int AS slots,
       count(*) FILTER (WHERE hvac_mode = 'off')::int AS off_slots,
       avg(outdoor_temp_f)::float8 AS outdoor,
       avg(zone_temp_f)::float8 AS zone,
       count(*) FILTER (WHERE hvac_mode IN ('cool', 'auto'))::int AS cool_mode_slots,
       count(hvac_mode)::int AS known_mode_slots
FROM runtime_5m
WHERE ts >= :t0 AND ts < :t1 {unit_filter}
GROUP BY unit_key, date_trunc('hour', ts, 'UTC')
"""


def local_day_ordinals(epochs: np.ndarray, t0: datetime, t1: datetime, tz: str) -> np.ndarray:
    """Local calendar day (date ordinal) of each UTC epoch in [t0, t1], exact across DST:
    searchsorted against the UTC starts of the local days."""
    from climate.timeutil import local_date

    d0, d1 = local_date(t0, tz), local_date(t1, tz)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 2)]
    starts = np.array([day_bounds_utc(d, tz)[0].timestamp() for d in days])
    idx = np.clip(np.searchsorted(starts, np.asarray(epochs, dtype=float), side="right") - 1, 0, len(days) - 1)
    return d0.toordinal() + idx.astype(np.int64)


def hourly_runtime(
    session: Session, t0: datetime, t1: datetime, tz: str, unit_keys: Iterable[str] | None = None,
    heat_is_comp: dict[str, bool] | None = None,
) -> HourlyRuntime:
    params: dict[str, object] = {"t0": t0, "t1": t1}
    unit_filter = ""
    if unit_keys is not None:
        unit_filter = "AND unit_key = ANY(:units)"
        params["units"] = list(unit_keys)
    rows = session.execute(text(_HOURLY_SQL.format(unit_filter=unit_filter)), params).all()
    heat_is_comp = heat_is_comp if heat_is_comp is not None else heat_metrics(session)
    n = len(rows)
    if n == 0:
        z = np.zeros(0)
        return HourlyRuntime(np.zeros(0, dtype=object), z, np.zeros(0, dtype=np.int64), z, z, z, z, z, z, z, z, z, z)
    rows.sort(key=lambda r: (r[0], r[1]))
    cols = list(zip(*rows, strict=True))
    unit = np.array(cols[0], dtype=object)
    f = lambda c: np.array([np.nan if v is None else v for v in c], dtype=float)
    hour = f(cols[1])
    heat1, aux1 = f(cols[3]), f(cols[4])
    comp = np.array([heat_is_comp.get(u, False) for u in cols[0]], dtype=bool)
    return HourlyRuntime(
        unit=unit,
        hour=hour,
        day=local_day_ordinals(hour, t0, t1, tz),
        cool=f(cols[2]),
        heat=np.where(comp, heat1, aux1),
        aux=np.where(comp, aux1, 0.0),
        fan=f(cols[5]),
        slots=f(cols[6]),
        off_slots=f(cols[7]),
        outdoor=f(cols[8]),
        zone=f(cols[9]),
        cool_mode_slots=f(cols[10]),
        known_mode_slots=f(cols[11]),
    )


_WEATHER_SQL = text(
    """
    SELECT DISTINCT ON (ts)
           extract(epoch FROM ts)::float8 AS hour_utc, temp_f::float8, shortwave_wm2::float8, source
    FROM weather_hourly
    WHERE kind = 'observed' AND temp_f IS NOT NULL AND ts >= :t0 AND ts < :t1
      AND source = ANY(CAST(:sources AS text[]))
    ORDER BY ts, array_position(CAST(:sources AS text[]), source)
    """
)


@dataclass
class HourlyOutdoor:
    """A dense UTC-hour grid of outdoor temperature (NaN where unknown after gap filling)."""

    start: float  # epoch seconds of the first hour
    temp: np.ndarray
    shortwave: np.ndarray  # NaN where the source had none
    source: np.ndarray  # object: source name per hour ('' when unknown, 'interpolated')

    def index(self, epoch_hours: np.ndarray) -> np.ndarray:
        return np.rint((np.asarray(epoch_hours, dtype=float) - self.start) / _HOUR).astype(np.int64)

    def at(self, epoch_hours: np.ndarray, which: str = "temp") -> np.ndarray:
        arr = self.temp if which == "temp" else self.shortwave
        idx = self.index(epoch_hours)
        ok = (idx >= 0) & (idx < arr.size)
        out = np.full(idx.shape, np.nan)
        out[ok] = arr[idx[ok]]
        return out


def hourly_outdoor(session: Session, t0: datetime, t1: datetime, runtime: HourlyRuntime | None = None) -> HourlyOutdoor:
    """Outdoor temperature per UTC hour in [t0, t1): weather_hourly observed by source
    preference, else the mean runtime_5m.outdoor_temp_f of the hour; gaps of up to
    MAX_INTERP_GAP_H hours between known values are filled linearly (CalTRACK practice)."""
    pad = timedelta(hours=MAX_INTERP_GAP_H)
    start = math.floor((t0 - pad).timestamp() / _HOUR) * _HOUR
    stop = math.ceil((t1 + pad).timestamp() / _HOUR) * _HOUR
    n = round((stop - start) / _HOUR)
    temp = np.full(n, np.nan)
    sw = np.full(n, np.nan)
    src = np.full(n, "", dtype=object)

    rows = session.execute(_WEATHER_SQL, {"t0": t0 - pad, "t1": t1 + pad, "sources": list(WEATHER_SOURCES)}).all()
    if rows:
        h, t, s, so = zip(*rows, strict=True)
        idx = np.rint((np.array(h) - start) / _HOUR).astype(np.int64)
        ok = (idx >= 0) & (idx < n)
        temp[idx[ok]] = np.array(t, dtype=float)[ok]
        sw[idx[ok]] = np.array([np.nan if v is None else v for v in s], dtype=float)[ok]
        src[idx[ok]] = np.array(so, dtype=object)[ok]

    # Fallback: the thermostats' own outdoor readings (mean over units for the hour).
    if runtime is not None and runtime.hour.size:
        fb = runtime.outdoor
        m = ~np.isnan(fb)
        if m.any():
            idx = np.rint((runtime.hour[m] - start) / _HOUR).astype(np.int64)
            ok = (idx >= 0) & (idx < n)
            sums = np.bincount(idx[ok], weights=fb[m][ok], minlength=n)
            cnts = np.bincount(idx[ok], minlength=n)
            fill = np.isnan(temp) & (cnts > 0)
            temp[fill] = sums[fill] / cnts[fill]
            src[fill] = RUNTIME_OUTDOOR

    # Linear interpolation across short interior gaps only (never extrapolate).
    known = np.flatnonzero(~np.isnan(temp))
    if known.size >= 2:
        gaps = np.diff(known)
        for k in np.flatnonzero((gaps > 1) & (gaps <= MAX_INTERP_GAP_H + 1)):
            a, b = known[k], known[k + 1]
            temp[a + 1 : b] = np.interp(np.arange(a + 1, b), [a, b], [temp[a], temp[b]])
            src[a + 1 : b] = "interpolated"
    return HourlyOutdoor(start=start, temp=temp, shortwave=sw, source=src)


# ---------------------------------------------------------------------------------------
# daily rows
# ---------------------------------------------------------------------------------------


def daily_rows(session: Session, start: date, end: date, tz: str, unit_keys: list[str] | None = None) -> list[DayRow]:
    """Rows for local days in [start, end]. Outdoor temperature: weather_hourly observed
    (open-meteo, else simulator), falling back to runtime_5m.outdoor_temp_f.

    One row per (unit, day) that has runtime data, ordered by unit then day."""
    if end < start:
        return []
    t0 = day_bounds_utc(start, tz)[0]
    t1 = day_bounds_utc(end, tz)[1]
    hr = hourly_runtime(session, t0, t1, tz, unit_keys)
    out = hourly_outdoor(session, t0, t1, hr)

    # Outdoor hours per local day (the hour's start decides the day).
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    day_weather: dict[int, tuple[list[float], list[str]]] = {}
    for d in days:
        a, b = day_bounds_utc(d, tz)
        i0, i1 = out.index(np.array([a.timestamp(), b.timestamp()]))
        i0, i1 = max(int(i0), 0), min(int(i1), out.temp.size)
        t = out.temp[i0:i1]
        ok = ~np.isnan(t)
        srcs = sorted({str(s) for s in out.source[i0:i1][ok] if s})
        day_weather[d.toordinal()] = ([round(float(v), 2) for v in t[ok]], srcs)
    exp_slots = {d.toordinal(): expected_slots(d, tz) for d in days}

    if hr.hour.size == 0:
        return []

    # Hourly duty for maxed-out minutes (duty over the slots present in that hour).
    covered = hr.slots * 300.0
    duty = np.divide(hr.stage1, covered, out=np.zeros_like(covered), where=covered > 0)
    maxed_min = np.where(duty >= MAXED_DUTY, hr.slots * 5.0, 0.0)

    # Group (unit, day) with integer keys.
    units, ucode = np.unique(hr.unit.astype(str), return_inverse=True)
    keys = ucode.astype(np.int64) * 10_000_000 + hr.day
    uniq, inverse = np.unique(keys, return_inverse=True)
    sums = {
        name: np.bincount(inverse, weights=arr, minlength=uniq.size)
        for name, arr in (("cool", hr.cool), ("heat", hr.heat), ("aux", hr.aux), ("fan", hr.fan),
                          ("slots", hr.slots), ("off", hr.off_slots), ("maxed", maxed_min))
    }

    rows: list[DayRow] = []
    for i, key in enumerate(uniq.tolist()):
        unit_key, ordinal = str(units[key // 10_000_000]), int(key % 10_000_000)
        if ordinal not in exp_slots:  # outside the requested days
            continue
        hourly, srcs = day_weather[ordinal]
        cool, heat, aux = float(sums["cool"][i]), float(sums["heat"][i]), float(sums["aux"][i])
        if cool <= 0 and heat + aux <= 0:
            mode = None
        else:
            mode = "cool" if cool >= heat + aux else "heat"
        row = DayRow(
            day=date.fromordinal(ordinal),
            unit_key=unit_key,
            cool_s=cool,
            heat_s=heat,
            aux_s=aux,
            fan_s=float(sums["fan"][i]),
            slots=int(sums["slots"][i]),
            mode=mode,
            outdoor_mean_f=round(float(np.mean(hourly)), 2) if hourly else None,
            outdoor_max_f=round(float(np.max(hourly)), 2) if hourly else None,
            hourly_outdoor_f=hourly,
            maxed_min=float(sums["maxed"][i]),
            expected_slots=exp_slots[ordinal],
            off_slots=int(sums["off"][i]),
            weather_sources=srcs,
        )
        cdd, hdd = day_degree_days(row, 65.0, "cool"), day_degree_days(row, 65.0, "heat")
        row.cdd65 = None if cdd is None else round(cdd, 3)
        row.hdd65 = None if hdd is None else round(hdd, 3)
        rows.append(row)
    rows.sort(key=lambda r: (unit_order(r.unit_key), r.day))
    return rows

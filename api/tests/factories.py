"""Deterministic synthetic history for tests (no simulator needed).

``make_history(session, days=30, end=...)`` writes weather_hourly (observed), runtime_5m for
the three units and readings_5m for every sensor, following simple physics-free formulas:
cooling runtime rises with outdoor temperature above a 65°F balance point; the upstairs unit
runs more when the main floor is warmer than the upstairs (the coupling the analytics should
find). Occupancy follows a fixed daily pattern per room.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.house import SENSORS
from climate.store.orm import Reading5m, Runtime5m, WeatherHour

BALANCE_F = 65.0
SLOPE_S_PER_DD = 1800.0  # seconds of runtime per cooling degree-day per unit
COUPLING_S_PER_DEGF = 9.0  # extra upstairs seconds per 5-min slot per °F main above up


def outdoor_f(ts: datetime, base: float = 82.0, swing: float = 11.0) -> float:
    """Diurnal sinusoid peaking at ~16:00 local-ish (UTC-5) plus a slow weekly wave."""
    hour = (ts.hour - 5) % 24 + ts.minute / 60
    day = ts.timetuple().tm_yday
    return base + swing * math.sin((hour - 10) / 24 * 2 * math.pi) + 3.0 * math.sin(day / 7 * 2 * math.pi)


def make_history(session: Session, days: int = 30, end: datetime | None = None, seed: int = 1) -> dict:
    rng = np.random.default_rng(seed)
    end = (end or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    weather, runtime, readings = [], [], []

    t = start
    while t < end:
        weather.append(
            dict(ts=t, source="open-meteo", kind="observed", temp_f=round(outdoor_f(t), 2), rh=55.0,
                 dewpoint_f=62.0, cloud_cover=30.0,
                 shortwave_wm2=max(0.0, 800 * math.sin(((t.hour - 5) % 24 - 6) / 12 * math.pi)), wind_mph=5.0,
                 precip_in=0.0)
        )
        t += timedelta(hours=1)

    t = start
    while t < end:
        out = outdoor_f(t)
        local_hour = (t.hour - 5) % 24
        main_empty_afternoon = 12 <= local_hour < 18 and t.weekday() < 5
        t_main = 76.0 + (3.5 if main_empty_afternoon else 0.0) + rng.normal(0, 0.2)
        t_up = 77.0 + rng.normal(0, 0.2)
        t_bed = 75.5 + rng.normal(0, 0.2)
        dd_slot = max(0.0, out - BALANCE_F) / 288.0
        base = SLOPE_S_PER_DD * dd_slot * 288.0 / 288.0
        main_s = min(300, max(0, base * (0.6 if main_empty_afternoon else 1.0) + rng.normal(0, 10)))
        up_s = min(300, max(0, base + COUPLING_S_PER_DEGF * max(0.0, t_main - t_up) + rng.normal(0, 10)))
        bed_s = min(300, max(0, base * 0.8 + rng.normal(0, 10)))
        for unit, secs, zone, cool_sp in (("main", main_s, t_main, 76.0), ("up", up_s, t_up, 77.0), ("bed", bed_s, t_bed, 75.0)):
            runtime.append(
                dict(ts=t, unit_key=unit, comp_cool1=int(secs), comp_cool2=int(secs * 0.2), fan=int(secs),
                     hvac_mode="cool", climate_ref="home", zone_temp_f=round(zone, 2), zone_humidity=48.0,
                     heat_sp_f=68.0, cool_sp_f=cool_sp, outdoor_temp_f=round(out, 2), outdoor_humidity=55.0,
                     source="ecobee_report")
            )
        for s in SENSORS:
            zone = {"main": t_main, "up": t_up, "bed": t_bed}[s.unit_key]
            occupied = None
            if s.has_occupancy:
                occupied = bool(7 <= local_hour < 22 and not (s.unit_key == "main" and main_empty_afternoon))
            readings.append(
                dict(ts=t, sensor_key=s.key, temp_f=round(zone + rng.normal(0, 0.3), 2),
                     humidity=48.0 if s.has_humidity else None, occupied=occupied, source="ecobee_report")
            )
        t += timedelta(minutes=5)

    for table, rows in ((WeatherHour, weather), (Runtime5m, runtime), (Reading5m, readings)):
        # Postgres allows at most 65,535 bind parameters per statement, and SQLAlchemy binds
        # every table column for each row of a multi-row insert.
        chunk = max(1, 60_000 // len(table.__table__.columns))
        for i in range(0, len(rows), chunk):
            session.execute(insert(table).values(rows[i : i + chunk]).on_conflict_do_nothing())
    session.flush()
    return {"start": start, "end": end, "weather": len(weather), "runtime": len(runtime), "readings": len(readings)}

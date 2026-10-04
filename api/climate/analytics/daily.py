"""Daily aggregation of runtime_5m + weather into the rows every analysis uses.

Runtime metric = stage-1 seconds. Cooling: comp_cool1. Heating: comp_heat1 for a heat pump,
aux_heat1 for a furnace (units.equipment['heating'] in {'heat_pump', 'furnace'}). When the
equipment is unknown: comp_heat1 if the unit ever reports compressor heat, else aux_heat1.
Never add the *2 columns (stage-1 already includes stage-2 time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session


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


def daily_rows(session: Session, start: date, end: date, tz: str, unit_keys: list[str] | None = None) -> list[DayRow]:
    """Rows for local days in [start, end]. Outdoor temperature: weather_hourly observed
    (open-meteo, else simulator), falling back to runtime_5m.outdoor_temp_f."""
    raise NotImplementedError


def degree_hours(hourly_outdoor_f: list[float], balance_point_f: float, mode: str) -> float:
    """Degree-days from hourly data: sum(max(0, T-bp))/24 for cool, sum(max(0, bp-T))/24 for heat."""
    raise NotImplementedError

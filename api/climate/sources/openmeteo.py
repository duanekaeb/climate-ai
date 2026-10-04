"""Open-Meteo (free, no key, non-commercial; CC BY 4.0 - show "Weather data by
Open-Meteo.com" wherever its data is shown). Forecast API for recent + next days, archive
API for backfill. Fahrenheit, mph, inches; timezone=GMT so hours are UTC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = "temperature_2m,relative_humidity_2m,dew_point_2m,cloud_cover,shortwave_radiation,wind_speed_10m,precipitation"


@dataclass
class WeatherHourIn:
    ts: datetime  # UTC hour start
    kind: str  # 'observed' (past hours) | 'forecast' (future hours)
    temp_f: float | None
    rh: float | None
    dewpoint_f: float | None
    cloud_cover: float | None
    shortwave_wm2: float | None
    wind_mph: float | None
    precip_in: float | None


async def fetch_forecast(lat: float, lon: float, now: datetime, past_days: int = 2, forecast_days: int = 3) -> list[WeatherHourIn]:
    raise NotImplementedError


async def fetch_archive(lat: float, lon: float, start: date, end: date) -> list[WeatherHourIn]:
    raise NotImplementedError

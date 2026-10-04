"""Open-Meteo (free, no key, non-commercial; CC BY 4.0 - show "Weather data by
Open-Meteo.com" wherever its data is shown). Forecast API for recent + next days, archive
API for backfill. Fahrenheit, mph, inches; timezone=GMT so hours are UTC.

Both fetchers return ``WeatherHourIn`` rows (UTC hour starts). Parsing is defensive: missing
arrays, nulls, short arrays and non-numeric values become ``None`` instead of raising.
Transient failures (HTTP 429/5xx, transport errors) are retried 3 times with backoff; a
persistent failure raises ``OpenMeteoError``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = "temperature_2m,relative_humidity_2m,dew_point_2m,cloud_cover,shortwave_radiation,wind_speed_10m,precipitation"

USER_AGENT = "climate-ai/0.1 (personal home climate optimizer; non-commercial)"
TIMEOUT_S = 20.0
RETRIES = 3  # retries after the first attempt
BACKOFF_S = 1.0  # first retry waits this long, then doubles
MAX_PAST_DAYS = 92
MAX_FORECAST_DAYS = 16
ARCHIVE_CHUNK_DAYS = 90

log = logging.getLogger(__name__)

UNITS_PARAMS = {
    "temperature_unit": "fahrenheit",
    "wind_speed_unit": "mph",
    "precipitation_unit": "inch",
    "timezone": "GMT",
}


class OpenMeteoError(RuntimeError):
    """Open-Meteo could not be reached or kept failing after the retries."""


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


# --- HTTP ------------------------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET with retries on 429/5xx and transport errors. Other 4xx fail at once."""
    delay = BACKOFF_S
    last_error = "no attempt"
    for attempt in range(RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise OpenMeteoError(f"Open-Meteo returned invalid JSON: {exc}") from exc
                if not isinstance(data, dict):
                    raise OpenMeteoError("Open-Meteo returned an unexpected payload")
                return data
            if resp.status_code != 429 and resp.status_code < 500:
                raise OpenMeteoError(f"Open-Meteo HTTP {resp.status_code}: {_reason(resp)}")
            last_error = f"HTTP {resp.status_code}"
        if attempt < RETRIES:
            log.info("Open-Meteo %s (attempt %d); retrying in %.1fs", last_error, attempt + 1, delay)
            await asyncio.sleep(delay)
            delay *= 2
    raise OpenMeteoError(f"Open-Meteo failed after {RETRIES + 1} attempts: {last_error}")


def _reason(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("reason"):
            return str(body["reason"])[:200]
    except ValueError:
        pass
    return resp.text[:200]


# --- parsing ---------------------------------------------------------------------------


def _num(values: Any, i: int) -> float | None:
    if not isinstance(values, list) or i >= len(values):
        return None
    v = values[i]
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_time(v: Any) -> datetime | None:
    """Open-Meteo times with timezone=GMT: 'YYYY-MM-DDTHH:MM' (UTC) or unix seconds."""
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ts = datetime.fromtimestamp(float(v), UTC)
        elif isinstance(v, str) and v:
            ts = datetime.fromisoformat(v)
            ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return ts.replace(minute=0, second=0, microsecond=0)


def parse_hourly(payload: dict[str, Any], now: datetime | None = None) -> list[WeatherHourIn]:
    """Turn an Open-Meteo response into hourly rows sorted by time.

    With ``now``: hours starting before ``now`` are 'observed', the rest 'forecast'.
    Without ``now`` every hour is 'observed' (archive). Duplicate hours keep the last value.
    """
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time")
    if not isinstance(times, list):
        return []
    out: dict[datetime, WeatherHourIn] = {}
    for i, raw in enumerate(times):
        ts = _parse_time(raw)
        if ts is None:
            continue
        kind = "observed" if now is None or ts < now else "forecast"
        rh = _num(hourly.get("relative_humidity_2m"), i)
        cloud = _num(hourly.get("cloud_cover"), i)
        sw = _num(hourly.get("shortwave_radiation"), i)
        wind = _num(hourly.get("wind_speed_10m"), i)
        precip = _num(hourly.get("precipitation"), i)
        out[ts] = WeatherHourIn(
            ts=ts,
            kind=kind,
            temp_f=_num(hourly.get("temperature_2m"), i),
            rh=None if rh is None else min(100.0, max(0.0, rh)),
            dewpoint_f=_num(hourly.get("dew_point_2m"), i),
            cloud_cover=None if cloud is None else min(100.0, max(0.0, cloud)),
            shortwave_wm2=None if sw is None else max(0.0, sw),
            wind_mph=None if wind is None else max(0.0, wind),
            precip_in=None if precip is None else max(0.0, precip),
        )
    return [out[k] for k in sorted(out)]


def _base_params(lat: float, lon: float) -> dict[str, Any]:
    return {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "hourly": HOURLY_VARS, **UNITS_PARAMS}


# --- public API ------------------------------------------------------------------------


async def fetch_forecast(
    lat: float,
    lon: float,
    now: datetime,
    past_days: int = 2,
    forecast_days: int = 3,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[WeatherHourIn]:
    """Recent past + upcoming hours from the forecast API. Hours before ``now`` are
    'observed', later hours 'forecast'. ``past_days`` <= 92, ``forecast_days`` <= 16."""
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    params = _base_params(lat, lon)
    params["past_days"] = max(0, min(MAX_PAST_DAYS, int(past_days)))
    params["forecast_days"] = max(1, min(MAX_FORECAST_DAYS, int(forecast_days)))
    if client is not None:
        payload = await _get_json(client, FORECAST_URL, params)
    else:
        async with _client() as c:
            payload = await _get_json(c, FORECAST_URL, params)
    return parse_hourly(payload, now=now)


async def fetch_archive(
    lat: float,
    lon: float,
    start: date,
    end: date,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[WeatherHourIn]:
    """Observed hours for the UTC calendar days ``start``..``end`` (inclusive) from the
    archive (ERA5 lags ~5 days). Long spans are fetched in 90-day chunks, one at a time."""
    if end < start:
        return []
    owned = client is None
    c = client or _client()
    try:
        rows: dict[datetime, WeatherHourIn] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=ARCHIVE_CHUNK_DAYS - 1))
            params = _base_params(lat, lon)
            params["start_date"] = chunk_start.isoformat()
            params["end_date"] = chunk_end.isoformat()
            payload = await _get_json(c, ARCHIVE_URL, params)
            for h in parse_hourly(payload, now=None):
                rows[h.ts] = h
            chunk_start = chunk_end + timedelta(days=1)
        return [rows[k] for k in sorted(rows)]
    finally:
        if owned:
            await c.aclose()

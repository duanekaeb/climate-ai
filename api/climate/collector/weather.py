"""Weather sync: Open-Meteo hourly into weather_hourly (observed past, forecast future).

``weather_hourly`` is keyed by (source, kind, ts): when an hour moves from the future into
the past, its old 'forecast' row stays next to the new 'observed' row, so forecast-based
decisions can be judged later. Rows from the simulator use source 'simulator' (written by
``SimulatedHouse`` when it runs on synthetic weather); real weather uses 'open-meteo'.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.sources import openmeteo
from climate.store.app_settings import LocationSettings, get_setting
from climate.store.db import session_scope
from climate.store.orm import WeatherHour

log = logging.getLogger(__name__)

SOURCE = "open-meteo"
ARCHIVE_LAG_DAYS = 5  # ERA5 archive lags about five days; newer days come from the forecast API
SYNC_PAST_DAYS = 2
SYNC_FORECAST_DAYS = 3
_FIELDS = ("temp_f", "rh", "dewpoint_f", "cloud_cover", "shortwave_wm2", "wind_mph", "precip_in")
_CHUNK = 2000


def _row(h: Any, source: str) -> dict[str, Any] | None:
    """One insert row from a WeatherHourIn, a dict or any object with the same fields."""
    get = h.get if isinstance(h, Mapping) else lambda k, d=None: getattr(h, k, d)
    ts, kind = get("ts"), get("kind")
    if not isinstance(ts, datetime) or kind not in ("observed", "forecast"):
        return None
    ts = (ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)).replace(
        minute=0, second=0, microsecond=0
    )
    row: dict[str, Any] = {"ts": ts, "source": source, "kind": kind}
    for f in _FIELDS:
        v = get(f)
        row[f] = None if v is None else float(v)
    return row


def upsert_weather(session: Session, hours: Iterable[Any], source: str) -> int:
    """Insert or update hourly rows for ``source``; returns the number of rows written.

    Conflicts on (source, kind, ts) overwrite the values and ``fetched_at``. Duplicate hours
    within one call keep the last one. Rows with a missing ts or an unknown kind are skipped.
    """
    rows: dict[tuple[str, datetime], dict[str, Any]] = {}
    for h in hours:
        row = _row(h, source)
        if row is not None:
            rows[(row["kind"], row["ts"])] = row
    values = list(rows.values())
    for i in range(0, len(values), _CHUNK):
        stmt = insert(WeatherHour).values(values[i : i + _CHUNK])
        stmt = stmt.on_conflict_do_update(
            index_elements=[WeatherHour.source, WeatherHour.kind, WeatherHour.ts],
            set_={**{f: getattr(stmt.excluded, f) for f in _FIELDS}, "fetched_at": func.now()},
        )
        session.execute(stmt)
    return len(values)


def _location() -> LocationSettings:
    with session_scope() as s:
        return get_setting(s, "location", LocationSettings)


def _store(hours: list[openmeteo.WeatherHourIn], source: str = SOURCE) -> int:
    with session_scope() as s:
        return upsert_weather(s, hours, source)


async def sync_weather(now: datetime) -> int:
    """Fetch forecast (past 2 days + next 3) for the configured location; upsert. When no
    location is set, return 0 (the simulator writes its own synthetic weather).
    Raises ``openmeteo.OpenMeteoError`` when Open-Meteo keeps failing."""
    loc = await asyncio.to_thread(_location)
    if loc.lat is None or loc.lon is None:
        return 0
    hours = await openmeteo.fetch_forecast(
        loc.lat, loc.lon, now, past_days=SYNC_PAST_DAYS, forecast_days=SYNC_FORECAST_DAYS
    )
    n = await asyncio.to_thread(_store, hours)
    log.info("weather: stored %d Open-Meteo hours", n)
    return n


async def backfill_weather(start: datetime, end: datetime, now: datetime | None = None) -> int:
    """Observed hours in [start, end) for the configured location: the archive for days
    older than 5 days, the forecast API's ``past_days`` (<= 92) for the recent part.
    Returns rows written (0 without a location)."""
    loc = await asyncio.to_thread(_location)
    if loc.lat is None or loc.lon is None or end <= start:
        return 0
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    cutoff: date = today - timedelta(days=ARCHIVE_LAG_DAYS)  # first day served by the forecast API
    first_day, last_day = (
        start.astimezone(UTC).date(),
        (end - timedelta(microseconds=1)).astimezone(UTC).date(),
    )

    hours: list[openmeteo.WeatherHourIn] = []
    if first_day < cutoff:
        hours += await openmeteo.fetch_archive(
            loc.lat, loc.lon, first_day, min(last_day, cutoff - timedelta(days=1))
        )
    if last_day >= cutoff:
        recent_from = max(first_day, cutoff)
        past_days = min(openmeteo.MAX_PAST_DAYS, (today - recent_from).days + 1)
        recent = await openmeteo.fetch_forecast(loc.lat, loc.lon, now, past_days=past_days, forecast_days=1)
        hours += [h for h in recent if h.ts.date() >= recent_from]
    keep = [h for h in hours if start <= h.ts < end and h.kind == "observed"]
    n = await asyncio.to_thread(_store, keep)
    log.info("weather: backfilled %d Open-Meteo hours", n)
    return n

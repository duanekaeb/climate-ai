"""Weather sync: Open-Meteo hourly into weather_hourly (observed past, forecast future)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session


async def sync_weather(now: datetime) -> int:
    """Fetch forecast (past 2 days + next 3) for the configured location; upsert. When no
    location is set, return 0 (the simulator writes its own synthetic weather)."""
    raise NotImplementedError


async def backfill_weather(start: datetime, end: datetime) -> int:
    raise NotImplementedError


def upsert_weather(session: Session, hours: list, source: str) -> int:
    raise NotImplementedError

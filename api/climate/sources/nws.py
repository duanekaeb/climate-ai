"""NWS api.weather.gov cross-check (no key; requires a descriptive User-Agent with contact).
Optional: latest observation from the nearest station, for a sanity check on Open-Meteo."""

from __future__ import annotations

from datetime import datetime


async def latest_observation(lat: float, lon: float, user_agent: str) -> tuple[datetime, float | None] | None:
    raise NotImplementedError

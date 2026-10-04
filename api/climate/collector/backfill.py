"""History backfill: ecobee runtimeReport in 31-day chunks (as far back as ecobee keeps,
one request at a time), Open-Meteo archive for the same span; or simulator history."""

from __future__ import annotations

from datetime import datetime

from climate.sources.base import ThermostatSource


async def backfill(source: ThermostatSource, start: datetime, end: datetime) -> dict:
    """Returns {'runtime_rows': n, 'weather_rows': n, 'start': ..., 'end': ...}."""
    raise NotImplementedError


async def backfill_simulator_if_empty(days: int) -> dict | None:
    """On first start in simulator mode with an empty runtime_5m: generate ``days`` of history
    (runtime, readings, room states, synthetic weather) so every screen has data."""
    raise NotImplementedError

"""NWS api.weather.gov cross-check (no key; requires a descriptive User-Agent with contact).
Optional: latest observation from the nearest station, for a sanity check on Open-Meteo.

Flow: ``/points/{lat},{lon}`` -> ``properties.observationStations`` -> the first station ->
``{station}/observations/latest``. Any failure (network, HTTP status, missing fields) returns
``None``; this is a best-effort cross-check, never a dependency.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

import httpx

BASE_URL = "https://api.weather.gov"
TIMEOUT_S = 20.0

log = logging.getLogger(__name__)


def _c_to_f(value: Any, unit_code: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    unit = str(unit_code or "wmoUnit:degC")
    if unit.endswith("degF"):
        return round(v, 2)
    return round(v * 9.0 / 5.0 + 32.0, 2)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> dict[str, Any]:
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise TypeError("unexpected NWS payload")
    return data


def _first_station_url(stations: dict[str, Any]) -> str | None:
    features = stations.get("features")
    if isinstance(features, list) and features:
        first = features[0]
        if isinstance(first, dict):
            sid = first.get("id")
            if isinstance(sid, str) and sid.startswith("http"):
                return sid
            ident = (first.get("properties") or {}).get("stationIdentifier")
            if isinstance(ident, str) and ident:
                return f"{BASE_URL}/stations/{ident}"
    urls = stations.get("observationStations")
    if isinstance(urls, list) and urls and isinstance(urls[0], str):
        return urls[0]
    return None


async def latest_observation(
    lat: float,
    lon: float,
    user_agent: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[datetime, float | None] | None:
    """(observation time UTC, temperature °F or None) from the nearest station, or None."""
    if not user_agent or not user_agent.strip():
        log.debug("NWS needs a User-Agent; skipping cross-check")
        return None
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    owned = client is None
    c = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        point = await _get(c, f"{BASE_URL}/points/{lat:.4f},{lon:.4f}", headers)
        stations_url = (point.get("properties") or {}).get("observationStations")
        if not isinstance(stations_url, str) or not stations_url:
            return None
        station_url = _first_station_url(await _get(c, stations_url, headers))
        if station_url is None:
            return None
        obs = await _get(c, f"{station_url.rstrip('/')}/observations/latest", headers)
        props = obs.get("properties") or {}
        ts = _parse_ts(props.get("timestamp"))
        if ts is None:
            return None
        temp = props.get("temperature") or {}
        temp_f = _c_to_f(temp.get("value"), temp.get("unitCode")) if isinstance(temp, dict) else None
        return ts, temp_f
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        log.info("NWS cross-check failed: %s", type(exc).__name__)
        return None
    finally:
        if owned:
            await c.aclose()

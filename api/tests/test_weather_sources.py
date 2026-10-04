"""Open-Meteo and NWS clients against httpx.MockTransport (no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from climate.sources import nws, openmeteo


def forecast_payload() -> dict:
    return {
        "latitude": 38.6,
        "longitude": -90.2,
        "hourly_units": {"temperature_2m": "°F"},
        "hourly": {
            "time": ["2026-10-04T10:00", "2026-10-04T11:00", "2026-10-04T12:00", "2026-10-04T13:00", "bogus"],
            "temperature_2m": [61.2, None, 64.0, "n/a", 1.0],
            "relative_humidity_2m": [80, 75, None, 60, 50],
            "dew_point_2m": [55.0, 54.1, 53.9, 52.0, 1.0],
            "cloud_cover": [100, 50, 0, 120],
            "shortwave_radiation": [0.0, 120.5, 300.0, -1.0, 0.0],
            "wind_speed_10m": [5.5, 6.0, 7.1, 8.0, 0.0],
            # precipitation missing entirely
        },
    }


def test_parse_hourly_splits_observed_and_forecast_and_tolerates_nulls():
    rows = openmeteo.parse_hourly(forecast_payload(), now=datetime(2026, 10, 4, 11, 30, tzinfo=UTC))
    assert [r.ts.hour for r in rows] == [10, 11, 12, 13]
    assert all(r.ts.tzinfo is not None for r in rows)
    assert [r.kind for r in rows] == ["observed", "observed", "forecast", "forecast"]
    assert rows[0].temp_f == 61.2 and rows[1].temp_f is None and rows[3].temp_f is None
    assert rows[2].rh is None
    assert rows[3].cloud_cover == 100.0  # clamped
    assert rows[3].shortwave_wm2 == 0.0  # clamped
    assert all(r.precip_in is None for r in rows)
    assert openmeteo.parse_hourly({"hourly": None}) == []
    assert openmeteo.parse_hourly({}) == []


async def test_fetch_forecast_params_and_split():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        seen["host"] = request.url.host
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json=forecast_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": openmeteo.USER_AGENT}
    ) as c:
        rows = await openmeteo.fetch_forecast(
            38.6, -90.2, datetime(2026, 10, 4, 12, 0, tzinfo=UTC), past_days=200, forecast_days=3, client=c
        )
    assert seen["host"] == "api.open-meteo.com"
    assert seen["temperature_unit"] == "fahrenheit" and seen["wind_speed_unit"] == "mph"
    assert seen["precipitation_unit"] == "inch" and seen["timezone"] == "GMT"
    assert seen["past_days"] == "92" and seen["forecast_days"] == "3"  # clamped to Open-Meteo's limit
    assert "shortwave_radiation" in seen["hourly"] and "climate-ai" in seen["ua"]
    assert [r.kind for r in rows] == [
        "observed",
        "observed",
        "forecast",
        "forecast",
    ]  # 12:00 == now -> forecast


async def test_retries_on_429_and_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(openmeteo, "BACKOFF_S", 0.0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"reason": "slow down"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=forecast_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        rows = await openmeteo.fetch_forecast(1, 2, datetime(2026, 10, 4, 12, tzinfo=UTC), client=c)
    assert len(calls) == 3 and len(rows) == 4


async def test_gives_up_after_retries_and_does_not_retry_4xx(monkeypatch):
    monkeypatch.setattr(openmeteo, "BACKOFF_S", 0.0)
    calls = []

    def always_500(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(always_500)) as c:
        with pytest.raises(openmeteo.OpenMeteoError):
            await openmeteo.fetch_forecast(1, 2, datetime(2026, 10, 4, tzinfo=UTC), client=c)
    assert len(calls) == openmeteo.RETRIES + 1

    calls.clear()

    def bad_request(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad_request)) as c:
        with pytest.raises(openmeteo.OpenMeteoError, match="Latitude"):
            await openmeteo.fetch_forecast(100, 2, datetime(2026, 10, 4, tzinfo=UTC), client=c)
    assert len(calls) == 1


async def test_fetch_archive_chunks_long_spans():
    ranges = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "archive-api.open-meteo.com"
        p = request.url.params
        ranges.append((p["start_date"], p["end_date"]))
        assert p["temperature_unit"] == "fahrenheit" and p["timezone"] == "GMT"
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [f"{p['start_date']}T00:00", f"{p['end_date']}T23:00"],
                    "temperature_2m": [50.0, 51.0],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        rows = await openmeteo.fetch_archive(38.6, -90.2, date(2026, 1, 1), date(2026, 7, 19), client=c)
    assert ranges == [
        ("2026-01-01", "2026-03-31"),
        ("2026-04-01", "2026-06-29"),
        ("2026-06-30", "2026-07-19"),
    ]
    assert len(rows) == 6 and all(r.kind == "observed" for r in rows)
    assert rows == sorted(rows, key=lambda r: r.ts)
    assert await openmeteo.fetch_archive(1, 2, date(2026, 2, 1), date(2026, 1, 1)) == []


def nws_handler(temp_value, fail_at: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("user-agent") == "climate-ai test (owner@example.com)"
        path = request.url.path
        if fail_at and fail_at in path:
            return httpx.Response(500)
        if path.startswith("/points/"):
            assert path == "/points/38.6270,-90.1994"
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "observationStations": "https://api.weather.gov/gridpoints/LSX/90,74/stations"
                    }
                },
            )
        if path.endswith("/stations"):
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "id": "https://api.weather.gov/stations/KSTL",
                            "properties": {"stationIdentifier": "KSTL"},
                        }
                    ]
                },
            )
        if path == "/stations/KSTL/observations/latest":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "timestamp": "2026-10-04T15:51:00+00:00",
                        "temperature": {"unitCode": "wmoUnit:degC", "value": temp_value},
                    }
                },
            )
        return httpx.Response(404)

    return handler


async def test_nws_latest_observation_converts_celsius():
    ua = "climate-ai test (owner@example.com)"
    async with httpx.AsyncClient(transport=httpx.MockTransport(nws_handler(25.0))) as c:
        got = await nws.latest_observation(38.627, -90.1994, ua, client=c)
    assert got == (datetime(2026, 10, 4, 15, 51, tzinfo=UTC), 77.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(nws_handler(None))) as c:
        _, temp = await nws.latest_observation(38.627, -90.1994, ua, client=c)
    assert temp is None


async def test_nws_returns_none_on_any_failure():
    ua = "climate-ai test (owner@example.com)"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(nws_handler(20.0, fail_at="/observations"))
    ) as c:
        assert await nws.latest_observation(38.627, -90.1994, ua, client=c) is None

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(garbage)) as c:
        assert await nws.latest_observation(38.627, -90.1994, ua, client=c) is None
    assert await nws.latest_observation(38.627, -90.1994, "") is None

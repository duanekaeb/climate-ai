"""Shared fixtures: an httpx.MockTransport router standing in for the API, and API payloads
shaped like climate.api.schemas. Nothing here touches the network or Claude."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from climate_agent.api import ApiClient
from climate_agent.config import AgentConfig

Route = Any  # dict / list / None payload, an httpx.Response, or a callable(request) -> either


class Router:
    """Maps (METHOD, /api/path) to a payload; records every request."""

    def __init__(self, routes: dict[tuple[str, str], Route]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"detail": f"no route for {key}"})
        route = self.routes[key]
        if callable(route):
            route = route(request)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    def bodies(self, method: str, path: str) -> list[dict[str, Any]]:
        return [
            json.loads(r.content or b"null")
            for r in self.requests
            if r.method == method and r.url.path == path
        ]

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


@pytest.fixture
def make_api() -> Callable[[dict[tuple[str, str], Route]], tuple[ApiClient, Router]]:
    def _make(routes: dict[tuple[str, str], Route]) -> tuple[ApiClient, Router]:
        router = Router(routes)
        return ApiClient("http://app:8000", "test-agent-token", transport=httpx.MockTransport(router)), router

    return _make


@pytest.fixture
def subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean subscription environment: OAuth token present, nothing that bills the API."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SIMPLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth-token-for-tests")


@pytest.fixture
def agent_config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        api_url="http://app:8000",
        agent_token="test-agent-token",
        oauth_token_present=True,
        cwd=tmp_path / "agent-empty",
        run_timeout_s=5.0,
    )


SETTINGS: dict[str, Any] = {
    "control": {
        "mode": "suggest",
        "comfort": {
            "main": {"day": {"heat_f": 68, "cool_f": 76}, "night": {"heat_f": 67, "cool_f": 74}, "away": {"heat_f": 62, "cool_f": 80}},
        },
        "limits": {"min_heat_f": 60, "max_heat_f": 74, "min_cool_f": 70, "max_cool_f": 84, "min_deadband_f": 3,
                   "max_step_f": 2, "min_minutes_between_changes": 30, "min_hold_hours": 1, "max_hold_hours": 2,
                   "max_indoor_rh": 58, "min_run_minutes": 5},
        "schedule": {},
        "hold_hours": 2,
        "resume_backoff_hours": 4.0,
        "manual_hold_reminder_hours": 3.0,
        "act_units": ["main", "up", "bed"],
    },
    "utility_events": {"alerts": True, "auto_skip": True, "skip_when_asleep": True, "skip_above_f": 79.5,
                       "skip_below_f": None, "precondition": True, "precondition_degrees_f": 2.0,
                       "precondition_hours": 2, "precondition_end_gap_min": 10},
    "occupancy": {"empty_after_min": 30, "house_empty_after_min": 45,
                  "sleep_windows": {"girls_room": [{"start": "20:30", "end": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]}]},
                  "phones_away": None},
    "location": {"tz": "America/Chicago", "confirmed": True},
    "policy": {"linked_floors_enabled": True, "linked_offset_f": 1.0, "recovery_lead_min": 20},
    "policy_version_id": 3,
    "signoff_ranges": {"linked_offset_f": [0.0, 3.0], "recovery_lead_min": [0, 45]},
    "owner_only_params": ["linked_floors_enabled", "precool_enabled", "bed_wing_independent"],
}

STATUS: dict[str, Any] = {
    "now": "2026-10-03T19:05:00Z",
    "tz": "America/Chicago",
    "source": {"kind": "simulator", "ok": True, "detail": ""},
    "homekit": {"enabled": False, "online": False, "paired": 0},
    "controller": {"mode": "suggest", "policy_version_id": 3, "policy": {}},
    "units": [
        {
            "unit_key": "main", "name": "Main floor", "call": "cool", "zone_temp_f": 76.24, "zone_humidity": 48.0,
            "heat_sp_f": 68.0, "cool_sp_f": 76.0, "connected": True,
            "hold": {"kind": "temperature", "heat_f": 68.0, "cool_f": 75.0, "end": "2026-10-03T20:30:00Z", "set_by_us": True},
            "hold_owner": "controller", "hold_label": "Our hold until 3:30 PM",
            "target": {"unit_key": "main", "heat_f": 68.0, "cool_f": 75.0, "rule": "linked_floors",
                       "reason": "Main floor empty, Toy Room occupied: main 1°F under upstairs"},
            "utility_event": {"id": 4, "unit_key": "main", "unit_name": "Main floor", "event_key": "link:peak-1",
                              "event_type": "demandResponse", "name": "Peak saver", "status": "announced",
                              "start_at": "2026-10-04T20:00:00Z", "end_at": "2026-10-04T23:00:00Z",
                              "first_seen_at": "2026-10-03T15:00:00Z", "is_optional": True,
                              "change_label": "cooling +2°F", "skip": "requested", "skip_by": "owner",
                              "skip_reason": "Skipped from the app", "can_skip": False, "prep_label": None},
            "upcoming_events": [
                {"event_type": "demandResponse", "name": "Peak saver", "running": False,
                 "start": "2026-10-04T20:00:00Z", "end": "2026-10-04T23:00:00Z"},
                {"event_type": "vacation", "name": "Trip", "running": False,
                 "start": "2026-10-10T13:00:00Z", "end": "2026-10-12T14:00:00Z"},
            ],
            "today_runtime_min": 182.4, "duty_last_hour_pct": 64.2, "maxed_minutes_today": 0.0,
        },
        {
            "unit_key": "up", "name": "Upstairs", "call": "cool", "zone_temp_f": 77.31, "zone_humidity": 51.0,
            "heat_sp_f": 68.0, "cool_sp_f": 77.0, "connected": True,
            "hold": {"kind": "temperature", "heat_f": 68.0, "cool_f": 74.0, "start": "2026-10-03T19:10:00Z",
                     "end": "2035-01-01T00:00:00Z", "hold_type": "indefinite", "set_by_us": False},
            "hold_owner": "person", "hold_label": "On your hold since 2:10 PM (until you change it)",
            "person_hold": {"since": "2026-10-03T19:10:00Z", "first_seen": "2026-10-03T19:12:00Z", "by": "thermostat",
                            "hold_type": "indefinite", "until": None, "heat_f": 68.0, "cool_f": 74.0, "detection_id": 9},
            "utility_event": {"id": 5, "unit_key": "up", "unit_name": "Upstairs", "event_key": "link:peak-1",
                              "event_type": "demandResponse", "name": "Peak saver", "status": "announced",
                              "start_at": "2026-10-04T20:00:00Z", "end_at": "2026-10-04T23:00:00Z",
                              "first_seen_at": "2026-10-03T15:00:00Z", "is_optional": True,
                              "change_label": "cooling +2°F", "skip": "requested", "skip_by": "owner",
                              "skip_reason": "Skipped from the app", "can_skip": False, "prep_label": None},
            "target": None,
            "today_runtime_min": 301.0, "duty_last_hour_pct": 100.0, "maxed_minutes_today": 45.0,
        },
        {
            "unit_key": "bed", "name": "Bed / Office wing", "call": "idle", "zone_temp_f": 74.0, "zone_humidity": 45.0,
            "heat_sp_f": 68.0, "cool_sp_f": 76.0, "connected": True, "hold": None, "hold_owner": None,
            "hold_label": None, "person_hold": None, "resume_backoff_until": "2026-10-03T23:10:00Z",
            "utility_event": {"id": 6, "unit_key": "bed", "unit_name": "Bed / Office wing", "event_key": "link:dr-9",
                              "event_type": "demandResponse", "name": "Grid emergency", "status": "opted_out",
                              "start_at": "2026-10-03T17:00:00Z", "end_at": "2026-10-03T21:00:00Z",
                              "first_seen_at": "2026-10-02T15:00:00Z", "is_optional": True,
                              "change_label": "AC off", "skip": "done", "skip_by": "rule",
                              "skip_reason": "Office reached 80°F", "can_skip": False, "prep_label": None},
            "target": None,
            "today_runtime_min": 120.0, "duty_last_hour_pct": 0.0, "maxed_minutes_today": 0.0,
        },
    ],
    "rooms": [
        {"room_key": "hallway", "name": "Hallway", "unit_key": "main", "floor": "main", "has_sensor": True,
         "is_sleep_room": False, "has_comfort_target": True, "temp_f": 75.84, "state": "empty", "is_priority": False},
        {"room_key": "twins_room", "name": "Twins' Room", "unit_key": "main", "floor": "main", "has_sensor": False,
         "is_sleep_room": True, "has_comfort_target": True, "temp_f": None, "state": "occupied", "is_priority": False},
        {"room_key": "toy_room", "name": "Toy Room", "unit_key": "up", "floor": "upstairs", "has_sensor": True,
         "is_sleep_room": False, "has_comfort_target": True, "temp_f": 77.2, "state": "occupied", "is_priority": True},
    ],
    "house_empty": False,
    "house_empty_reason": "motion in the Toy Room 4 min ago",
    "weather": {"temp_f": 88.4, "rh": 40.0, "cloud_cover": 20.0, "forecast_high_f": 93.0, "forecast_low_f": 70.0,
                "source": "open-meteo", "attribution": "Weather data by Open-Meteo.com"},
    "alerts": [{"id": 1, "ts": "2026-10-03T18:50:00Z", "level": "warn", "kind": "maxed",
                "title": "Upstairs at 100% duty for 45 min", "body": ""}],
    "agent": {"enabled": True, "signed_in": True, "settings": {}},
    "location_confirmed": True,
}

SAVINGS: dict[str, Any] = {
    "start": "2026-09-19", "end": "2026-10-02", "n_days": 14,
    "expected_min": 4200.0, "actual_min": 3900.0, "savings_min": 300.0, "savings_pct": 7.142,
    "ci90_low_pct": 2.04, "ci90_high_pct": 12.31,
    "by_unit": [{"unit_key": "main", "expected_min": 1400.0, "actual_min": 1200.0, "savings_pct": 14.29}],
    "days": [], "baseline_ok": True, "note": "Weather-normalized with CalTRACK-style baselines.",
}


def change(cid: int, needs: str, params: dict[str, Any], status: str = "awaiting_signoff") -> dict[str, Any]:
    return {
        "id": cid, "created_at": "2026-09-28T08:00:00Z", "kind": "policy",
        "title": f"Change {cid}", "rationale": "Backtest beats uncertainty.", "payload": {"params": params},
        "proposed_by": "model", "status": status,
        "gates": {"backtest": {"delta_pct": -4.21, "ci90_pct": [-6.9, -1.5], "beats_model_uncertainty": True},
                  "shadow": {"days": 3, "comfort_regressions": 0}},
        "needs": needs,
    }


@pytest.fixture(autouse=True)
def no_sessions_on_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume checks never read ~/.claude in tests: no session is on disk unless a test says so."""
    from climate_agent import runner

    monkeypatch.setattr(runner, "session_exists", lambda session_id, cwd: False)

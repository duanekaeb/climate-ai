"""The role matrix from docs/BUILD.md: every route is public, reader, writer, owner or agent.

The agent may never call /control/* writes, /setup/*, /agent/ask|run,
/experiments/{id}/decision or /alerts/*/resolve. Readers need a session or a token.
"""

from __future__ import annotations

import pytest

PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/state"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}

READER = [
    ("GET", "/api/status"),
    ("GET", "/api/rooms/{room_key}/history"),
    ("GET", "/api/runtime/daily"),
    ("GET", "/api/runtime/intraday"),
    ("GET", "/api/weather"),
    ("GET", "/api/analytics/savings"),
    ("GET", "/api/analytics/waterfall"),
    ("GET", "/api/analytics/baselines"),
    ("GET", "/api/analytics/coupling"),
    ("GET", "/api/analytics/comfort"),
    ("GET", "/api/analytics/drift"),
    ("GET", "/api/analytics/natural-experiments"),
    ("GET", "/api/control/settings"),
    ("GET", "/api/control/plan"),
    ("GET", "/api/control/actions"),
    ("GET", "/api/changes"),
    ("GET", "/api/experiments"),
    ("GET", "/api/experiments/power"),
    ("GET", "/api/experiments/{experiment_id}"),
    ("GET", "/api/models"),
    ("GET", "/api/jobs/{job_id}"),
    ("GET", "/api/reports"),
    ("GET", "/api/reports/{report_id}"),
    ("GET", "/api/alerts"),
    ("GET", "/api/agent/status"),
    ("GET", "/api/agent/runs"),
    ("GET", "/api/agent/runs/{run_id}"),
]

WRITER = [
    ("POST", "/api/changes"),
    ("POST", "/api/changes/{change_id}/decision"),
    ("POST", "/api/experiments"),
    ("POST", "/api/models/refit"),
    ("POST", "/api/models/backtest"),
    ("POST", "/api/models/simulate"),
    ("POST", "/api/reports"),
]

OWNER_ONLY = [
    ("PUT", "/api/control/settings"),
    ("POST", "/api/control/mode"),
    ("POST", "/api/control/hold"),
    ("POST", "/api/control/resume"),
    ("POST", "/api/control/presence"),
    ("POST", "/api/experiments/{experiment_id}/decision"),
    ("POST", "/api/alerts/{alert_id}/resolve"),
    ("POST", "/api/agent/ask"),
    ("POST", "/api/agent/run"),
    ("GET", "/api/setup"),
    ("POST", "/api/setup/source"),
    ("PUT", "/api/setup/location"),
    ("POST", "/api/setup/ecobee/login"),
    ("POST", "/api/setup/ecobee/mfa"),
    ("POST", "/api/setup/ecobee/signout"),
    ("POST", "/api/setup/ecobee/map"),
    ("POST", "/api/setup/sensors/map"),
    ("POST", "/api/setup/homekit/pair"),
    ("POST", "/api/setup/homekit/code"),
    ("POST", "/api/setup/homekit/unpair"),
]

AGENT_ONLY = [
    ("POST", "/api/agent/claim"),
    ("POST", "/api/agent/runs/{run_id}/finish"),
    ("POST", "/api/agent/heartbeat"),
]

SAMPLE = {"room_key": "hallway", "experiment_id": "1", "job_id": "1", "report_id": "1", "run_id": "1",
          "change_id": "1", "alert_id": "1"}


def concrete(path: str) -> str:
    return path.format(**SAMPLE)


def test_every_route_is_classified():
    from climate.api.app import create_app

    spec = create_app().openapi()
    routes = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    classified = PUBLIC | set(READER) | set(WRITER) | set(OWNER_ONLY) | set(AGENT_ONLY)
    assert routes == classified, f"unclassified: {routes - classified}; stale: {classified - routes}"


@pytest.mark.parametrize(("method", "path"), OWNER_ONLY)
def test_agent_is_forbidden_on_owner_routes(agent, method, path):
    r = agent.request(method, concrete(path), json={})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize(("method", "path"), AGENT_ONLY)
def test_owner_is_forbidden_on_agent_routes(owner, method, path):
    r = owner.request(method, concrete(path), json={})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize(("method", "path"), READER + WRITER + OWNER_ONLY + AGENT_ONLY)
def test_anonymous_gets_401(client, method, path):
    r = client.request(method, concrete(path), json={})
    assert r.status_code == 401, r.text


def test_agent_reads_status_and_settings(agent):
    assert agent.get("/api/status").status_code == 200
    assert agent.get("/api/control/settings").status_code == 200
    assert agent.get("/api/agent/status").status_code == 200

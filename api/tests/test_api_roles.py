"""What each role can actually do, with real request bodies (the exhaustive every-route matrix
is tests/test_route_guard.py). Spec: docs/specs/users-and-tokens.md, "Who may call what".

- owner: everything except the agent's own run bookkeeping
- control token: reads + everyday controls (hold, resume, back to automatic, presence, skip a
  utility event); never mode, settings, hand-back, proposals, setup or tokens
- agent token (``cai_`` or the legacy env token): reads + the gated agent writes (propose, sign
  off, reports, refits) + its own runs; never a thermostat write, mode, settings or setup
- viewer token: reads only
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from climate.store.db import session_scope
from climate.store.orm import ControlAction
from tests.conftest import make_client

HOLD = {"unit_key": "main", "heat_f": 68, "cool_f": 76}


def code(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


@pytest.fixture
def agent_api(agent_token):
    """The agent with an API token from the app (instead of the legacy env token)."""
    with make_client(token=agent_token) as c:
        yield c


def test_control_token_runs_everyday_controls(control):
    r = control.post("/api/control/hold", json=HOLD)
    assert r.status_code == 200, r.text
    assert control.post("/api/control/resume", json={"unit_key": "main"}).status_code == 200
    assert control.post("/api/control/automatic", json={"unit_key": "main"}).status_code == 200
    assert control.post("/api/control/presence", json={"phones_away": True}).json()["occupancy"]["phones_away"] is True
    # auth passes for the utility-event skip; the event simply does not exist
    assert control.post("/api/utility-events/999/skip", json={}).status_code == 404
    with session_scope() as s:
        kinds = [a.action for a in s.execute(select(ControlAction).order_by(ControlAction.id)).scalars()]
    assert kinds == ["set_hold", "resume_program", "resume_program"]


def test_control_token_cannot_change_how_the_house_is_run(control):
    for method, path, body in [
        ("POST", "/api/control/mode", {"mode": "act"}),
        ("PUT", "/api/control/settings", {}),
        ("POST", "/api/control/handback", None),
        ("POST", "/api/changes", {"title": "x", "params": {"linked_offset_f": 1}}),
        ("POST", "/api/agent/ask", {"question": "hi"}),
        ("GET", "/api/setup", None),
        ("GET", "/api/tokens", None),
        ("GET", "/api/audit", None),
    ]:
        r = control.request(method, path, json=body)
        assert r.status_code == 403 and code(r) == "FORBIDDEN", (method, path, r.text)


@pytest.mark.parametrize("who", ["agent", "agent_api"])
def test_agent_reads_and_makes_gated_writes_only(request, who):
    c = request.getfixturevalue(who)
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/control/settings").status_code == 200
    assert c.get("/api/agent/status").status_code == 200
    assert c.post("/api/agent/heartbeat", json={}).status_code == 200
    assert c.post("/api/agent/claim").status_code == 200
    r = c.post("/api/reports", json={"kind": "note", "title": "Filter", "body_md": "ok"})
    assert r.status_code == 200 and r.json()["author"] == "claude"
    for method, path, body in [
        ("POST", "/api/control/hold", HOLD),
        ("POST", "/api/control/resume", {"unit_key": "main"}),
        ("POST", "/api/control/automatic", {"unit_key": "main"}),
        ("POST", "/api/control/presence", {"phones_away": False}),
        ("POST", "/api/control/mode", {"mode": "act"}),
        ("POST", "/api/utility-events/1/skip", {}),
        ("GET", "/api/setup", None),
        ("POST", "/api/tokens", {"name": "more", "role": "control"}),
    ]:
        r = c.request(method, path, json=body)
        assert r.status_code == 403 and code(r) == "FORBIDDEN", (who, method, path, r.text)
    with session_scope() as s:
        assert s.execute(select(ControlAction)).first() is None


def test_viewer_token_reads_only(viewer):
    assert viewer.get("/api/status").status_code == 200
    assert viewer.get("/api/analytics/savings").status_code == 200
    for method, path, body in [
        ("POST", "/api/control/hold", HOLD),
        ("POST", "/api/control/presence", {"phones_away": True}),
        ("POST", "/api/reports", {"kind": "note", "title": "x", "body_md": "x"}),
        ("POST", "/api/models/refit", None),
        ("POST", "/api/agent/claim", None),
    ]:
        r = viewer.request(method, path, json=body)
        assert r.status_code == 403 and code(r) == "FORBIDDEN", (method, path, r.text)


def test_owner_is_not_the_agent_service(owner):
    for path in ("/api/agent/claim", "/api/agent/heartbeat", "/api/agent/runs/1/finish"):
        r = owner.post(path, json={})
        assert r.status_code == 403 and code(r) == "FORBIDDEN", path
    assert owner.post("/api/control/hold", json=HOLD).status_code == 200


def test_anonymous_is_asked_to_sign_in(client):
    r = client.get("/api/status")
    assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"
    assert r.headers["www-authenticate"] == "Bearer"

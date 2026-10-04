"""Control routes: settings validation, mode, owner holds (validated, queued), presence, the
action log, and the change gates' permission mapping (control.changes is monkeypatched)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from climate.api.schemas import PlanRow
from climate.control import changes, controller
from climate.control.guardrails import GuardResult
from climate.control.policy import UnitTarget
from climate.store.app_settings import SourceSettings, put_setting
from climate.store.db import session_scope
from climate.store.orm import Change, ControlAction


def _messages(r) -> str:
    return " | ".join(d["msg"] for d in r.json()["detail"])


def _settings(client) -> dict:
    r = client.get("/api/control/settings")
    assert r.status_code == 200
    return r.json()


# --- settings ----------------------------------------------------------------------------


def test_settings_readable_by_both_roles(owner, agent):
    body = _settings(agent)
    assert body["control"]["mode"] == "suggest"
    assert set(body["control"]["comfort"]) == {"main", "up", "bed"}
    assert "linked_offset_f" in body["signoff_ranges"]
    assert "linked_floors_enabled" in body["owner_only_params"]
    assert body["policy_version_id"] is not None  # the seeded active policy
    assert _settings(owner)["policy"]["linked_offset_f"] == 1.0


def test_put_settings_saves_and_round_trips(owner):
    current = _settings(owner)
    control = current["control"]
    control["comfort"]["main"]["day"]["cool_f"] = 77.0
    control["hold_hours"] = 1
    r = owner.put("/api/control/settings", json={"control": control})
    assert r.status_code == 200, r.text
    assert r.json()["control"]["comfort"]["main"]["day"]["cool_f"] == 77.0
    assert _settings(owner)["control"]["hold_hours"] == 1


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda c: c["comfort"]["main"]["day"].update(heat_f=75.0, cool_f=76.0), "at least 3.0°F below cool"),
        (lambda c: c["comfort"]["up"]["away"].update(cool_f=88.0), "outside the hard limits"),
        (lambda c: c["comfort"]["bed"]["night"].update(heat_f=58.0), "outside the hard limits"),
        (lambda c: c["comfort"].pop("bed"), "missing for: bed"),
        (lambda c: c["limits"].update(min_heat_f=75.0, max_heat_f=70.0), "min_heat_f is above max_heat_f"),
        (lambda c: c["limits"].update(max_hold_hours=4), "Holds are 1-2 hours"),
        (lambda c: c.update(act_units=["main", "attic"]), "Unknown units: attic"),
    ],
)
def test_put_settings_rejects_invalid_control(owner, mutate, needle):
    before = _settings(owner)["control"]
    control = copy.deepcopy(before)
    mutate(control)
    r = owner.put("/api/control/settings", json={"control": control})
    assert r.status_code == 422, r.text
    assert needle in _messages(r)
    assert _settings(owner)["control"] == before  # nothing saved


def test_put_settings_rejects_bad_location_and_rooms(owner):
    body = _settings(owner)
    loc = dict(body["location"], tz="Mars/Olympus_Mons")
    r = owner.put("/api/control/settings", json={"location": loc})
    assert r.status_code == 422 and "Unknown time zone" in _messages(r)
    occ = body["occupancy"]
    occ["sleep_windows"]["attic"] = [{"start": "20:00", "end": "07:00"}]
    r = owner.put("/api/control/settings", json={"occupancy": occ})
    assert r.status_code == 422 and "Unknown rooms: attic" in _messages(r)
    ok = owner.put("/api/control/settings", json={"location": dict(body["location"], lat=41.9, lon=-87.6)})
    assert ok.status_code == 200 and ok.json()["location"]["lat"] == 41.9


def test_mode_switch(owner):
    r = owner.post("/api/control/mode", json={"mode": "act"})
    assert r.status_code == 200
    assert r.json()["mode"] == "act"
    assert r.json()["policy"]["linked_floors_enabled"] is True
    assert _settings(owner)["control"]["mode"] == "act"
    assert owner.post("/api/control/mode", json={"mode": "auto"}).status_code == 422


def test_plan_wraps_controller(owner, monkeypatch):
    target = UnitTarget(unit_key="up", heat_f=68, cool_f=77, rule="comfort", reason="Toy Room occupied")
    row = PlanRow(target=target, guard=GuardResult(ok=True, heat_f=68, cool_f=77), current_heat_f=68,
                  current_cool_f=78, would_write=True)
    monkeypatch.setattr(controller, "current_plan", lambda session, now=None: [row])
    r = owner.get("/api/control/plan")
    assert r.status_code == 200
    assert r.json()["mode"] == "suggest"
    assert r.json()["rows"][0]["target"]["reason"] == "Toy Room occupied"


# --- owner holds -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"unit_key": "main", "heat_f": 55, "cool_f": 76}, "Heat 55.0°F is outside the hard limits"),
        ({"unit_key": "main", "heat_f": 68, "cool_f": 90}, "Cool 90.0°F is outside the hard limits"),
        ({"unit_key": "main", "heat_f": 72, "cool_f": 73}, "at least 3.0°F below cool"),
    ],
)
def test_manual_hold_validates_hard_limits(owner, body, needle):
    r = owner.post("/api/control/hold", json=body)
    assert r.status_code == 422, r.text
    assert needle in _messages(r)
    with session_scope() as s:
        assert s.execute(select(ControlAction)).first() is None


def test_manual_hold_unknown_unit_and_bad_hours(owner):
    assert owner.post("/api/control/hold", json={"unit_key": "attic", "heat_f": 68, "cool_f": 76}).status_code == 404
    assert owner.post("/api/control/hold", json={"unit_key": "main", "heat_f": 68, "cool_f": 76,
                                                 "hours": 3}).status_code == 422


def test_manual_hold_is_queued_on_the_current_source(owner):
    r = owner.post("/api/control/hold", json={"unit_key": "up", "heat_f": 67.8, "cool_f": 76.3, "hours": 1})
    assert r.status_code == 200, r.text
    a = r.json()
    assert (a["actor"], a["mode"], a["channel"], a["action"], a["status"]) == (
        "owner", "act", "simulator", "set_hold", "queued")
    assert a["request"]["heat_f"] == 68.0 and a["request"]["cool_f"] == 76.5  # rounded to 0.5°F
    assert a["request"]["hours"] == 1 and a["request"]["unit_key"] == "up"

    with session_scope() as s:
        put_setting(s, "source", SourceSettings(kind="ecobee"))
    r = owner.post("/api/control/resume", json={"unit_key": "bed"})
    assert r.status_code == 200
    assert (r.json()["channel"], r.json()["action"], r.json()["status"]) == ("ecobee", "resume_program", "queued")

    log = owner.get("/api/control/actions").json()
    assert [x["action"] for x in log] == ["resume_program", "set_hold"]
    assert [x["unit_key"] for x in owner.get("/api/control/actions", params={"unit_key": "up"}).json()] == ["up"]
    assert owner.get("/api/control/actions", params={"unit_key": "attic"}).status_code == 404


def test_presence(owner):
    r = owner.post("/api/control/presence", json={"phones_away": True})
    assert r.status_code == 200
    occ = r.json()["occupancy"]
    assert occ["phones_away"] is True and occ["phones_updated_at"] is not None
    assert owner.post("/api/control/presence", json={"phones_away": None}).json()["occupancy"]["phones_away"] is None


# --- change gates --------------------------------------------------------------------------


def _insert_change(status: str = "awaiting_signoff", proposed_by: str = "model") -> int:
    with session_scope() as s:
        c = Change(kind="policy", title="Tune offset", rationale="r", payload={"params": {"linked_offset_f": 1.5}},
                   proposed_by=proposed_by, status=status, gates={})
        s.add(c)
        s.flush()
        return c.id


@pytest.fixture
def fake_needs(monkeypatch):
    monkeypatch.setattr(changes, "needs", lambda c: "claude" if c.status == "awaiting_signoff" else "nothing")


def test_list_changes_with_status_filter(owner, fake_needs):
    a = _insert_change("awaiting_signoff")
    _insert_change("held")
    rows = owner.get("/api/changes").json()
    assert len(rows) == 2
    only = owner.get("/api/changes", params={"status": "awaiting_signoff"}).json()
    assert [c["id"] for c in only] == [a] and only[0]["needs"] == "claude"
    assert owner.get("/api/changes", params={"status": "bogus"}).status_code == 422


@pytest.mark.parametrize(("who", "expected"), [("agent", "claude"), ("owner", "owner")])
def test_propose_maps_role_to_proposer(request, monkeypatch, fake_needs, who, expected):
    client = request.getfixturevalue(who)
    seen = {}

    def fake_propose(session, proposed_by, title, rationale, params):
        seen.update(proposed_by=proposed_by, params=params)
        c = Change(kind="policy", title=title, rationale=rationale, payload={"params": params},
                   proposed_by=proposed_by, status="backtest", gates={})
        session.add(c)
        session.flush()
        return c

    monkeypatch.setattr(changes, "propose_policy_change", fake_propose)
    r = client.post("/api/changes", json={"title": "Wider offset", "params": {"linked_offset_f": 1.5}})
    assert r.status_code == 200, r.text
    assert seen == {"proposed_by": expected, "params": {"linked_offset_f": 1.5}}
    assert r.json()["proposed_by"] == expected and r.json()["status"] == "backtest"
    assert client.post("/api/changes", json={"title": "Nothing", "params": {}}).status_code == 422


@pytest.mark.parametrize(("who", "actor"), [("agent", "claude"), ("owner", "owner")])
def test_decision_maps_role_to_actor(request, monkeypatch, fake_needs, who, actor):
    client = request.getfixturevalue(who)
    cid = _insert_change()
    seen = {}

    def fake_decide(session, change_id, actor, decision, reason):
        seen.update(change_id=change_id, actor=actor, decision=decision)
        c = session.get(Change, change_id)
        c.status, c.decided_by, c.decision_reason = "trial", actor, reason
        c.decided_at = datetime.now(UTC)
        return c

    monkeypatch.setattr(changes, "decide", fake_decide)
    r = client.post(f"/api/changes/{cid}/decision", json={"decision": "approve", "reason": "inside the range"})
    assert r.status_code == 200, r.text
    assert seen == {"change_id": cid, "actor": actor, "decision": "approve"}
    assert r.json()["decided_by"] == actor and r.json()["needs"] == "nothing"


def test_decision_errors_map_to_http(agent, monkeypatch, fake_needs):
    cid = _insert_change()

    def forbidden(*a, **k):
        raise PermissionError("Claude may not sign off a change Claude proposed.")

    monkeypatch.setattr(changes, "decide", forbidden)
    r = agent.post(f"/api/changes/{cid}/decision", json={"decision": "approve", "reason": "looks fine"})
    assert r.status_code == 403 and "may not sign off" in r.json()["detail"]

    def wrong_state(*a, **k):
        raise ValueError("Change is not awaiting a decision.")

    monkeypatch.setattr(changes, "decide", wrong_state)
    r = agent.post(f"/api/changes/{cid}/decision", json={"decision": "hold", "reason": "wait a week"})
    assert r.status_code == 409
    assert agent.post("/api/changes/9999/decision", json={"decision": "hold", "reason": "nope"}).status_code == 404
    assert agent.post(f"/api/changes/{cid}/decision", json={"decision": "maybe", "reason": "x" * 5}).status_code == 422

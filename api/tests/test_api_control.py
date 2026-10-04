"""Control routes: settings validation (including utility events and the renamed resume
back-off), mode, owner holds (validated against the hard envelope only, queued), "Resume
schedule" and "Back to automatic", presence, the action log, and the change gates' permission
mapping (control.changes is monkeypatched)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from climate.api.schemas import PlanRow
from climate.control import changes, controller
from climate.control.guardrails import GuardResult
from climate.control.policy import UnitTarget
from climate.sources.base import UnitSnapshot
from climate.store.app_settings import ControlSettings, SourceSettings, get_setting, put_setting
from climate.store.db import session_scope
from climate.store.orm import Change, ControlAction, LiveUnit
from climate.timeutil import utcnow


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


def test_utility_event_settings_round_trip(owner, agent):
    body = _settings(agent)
    assert body["utility_events"]["alerts"] is True and body["utility_events"]["precondition"] is False
    ue = dict(body["utility_events"], auto_skip=True, skip_above_f=79.5, precondition=True,
              precondition_degrees_f=1.5, precondition_end_gap_min=15)
    r = owner.put("/api/control/settings", json={"utility_events": ue})
    assert r.status_code == 200, r.text
    saved = _settings(owner)["utility_events"]
    assert (saved["auto_skip"], saved["skip_above_f"], saved["precondition_degrees_f"], saved["precondition_end_gap_min"]) \
        == (True, 79.5, 1.5, 15)
    assert _settings(owner)["control"] == body["control"]  # other sections untouched
    for bad in ({"precondition_degrees_f": 5}, {"precondition_end_gap_min": 5}, {"skip_above_f": 60}):
        r = owner.put("/api/control/settings", json={"utility_events": dict(ue, **bad)})
        assert r.status_code == 422, bad
    assert _settings(owner)["utility_events"] == saved  # nothing saved


def test_resume_backoff_and_reminder_settings(owner):
    control = _settings(owner)["control"]
    assert control["resume_backoff_hours"] == 4.0 and control["manual_hold_reminder_hours"] == 0.0
    assert "manual_backoff_hours" not in control
    r = owner.put("/api/control/settings", json={"control": dict(control, resume_backoff_hours=2.5,
                                                                 manual_hold_reminder_hours=3)})
    assert r.status_code == 200, r.text
    assert (r.json()["control"]["resume_backoff_hours"], r.json()["control"]["manual_hold_reminder_hours"]) == (2.5, 3)
    assert owner.put("/api/control/settings", json={"control": dict(control, resume_backoff_hours=30)}).status_code == 422


def test_settings_accept_the_old_backoff_name(owner):
    control = _settings(owner)["control"]
    legacy = {k: v for k, v in control.items() if k != "resume_backoff_hours"}
    r = owner.put("/api/control/settings", json={"control": dict(legacy, manual_backoff_hours=6)})
    assert r.status_code == 200, r.text
    assert r.json()["control"]["resume_backoff_hours"] == 6.0
    # an old web bundle spreads what it read (the new name) and sets the old one: the old one is the edit
    r = owner.put("/api/control/settings", json={"control": dict(control, manual_backoff_hours=1.5)})
    assert r.status_code == 200, r.text
    assert r.json()["control"]["resume_backoff_hours"] == 1.5
    with session_scope() as s:
        assert get_setting(s, "control", ControlSettings).resume_backoff_hours == 1.5
    assert owner.put("/api/control/settings", json={"control": dict(legacy, manual_backoff_hours=99)}).status_code == 422


def test_mode_switch(owner):
    r = owner.post("/api/control/mode", json={"mode": "act"})
    assert r.status_code == 200
    assert r.json()["mode"] == "act"
    assert r.json()["policy"]["linked_floors_enabled"] is True
    assert _settings(owner)["control"]["mode"] == "act"
    assert owner.post("/api/control/mode", json={"mode": "auto"}).status_code == 422


def test_put_settings_never_changes_the_mode(owner):
    """Only POST /control/mode (and the worker's hand-back) changes the mode: a form loaded
    before a hand-back switched the controller off saves its stale copy without switching it
    back on, and its own change still lands."""
    assert owner.post("/api/control/mode", json={"mode": "act"}).status_code == 200
    stale = _settings(owner)["control"]  # the Guardrails page, loaded in Act
    with session_scope() as s:  # the hand-back's first step, in the worker
        current = get_setting(s, "control", ControlSettings)
        put_setting(s, "control", current.model_copy(update={"mode": "off"}), updated_by="owner")
    stale["hold_hours"] = 1
    r = owner.put("/api/control/settings", json={"control": stale})
    assert r.status_code == 200, r.text
    assert r.json()["control"]["mode"] == "off" and r.json()["control"]["hold_hours"] == 1
    with session_scope() as s:
        stored = get_setting(s, "control", ControlSettings)
    assert stored.mode == "off" and stored.hold_hours == 1

    # the other way round: a stale 'off' does not switch off a controller set to Suggest since
    assert owner.post("/api/control/mode", json={"mode": "suggest"}).status_code == 200
    assert owner.put("/api/control/settings", json={"control": stale}).json()["control"]["mode"] == "suggest"


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


def _live(unit_key: str, **kw) -> None:
    snap = UnitSnapshot(unit_key=unit_key, ts=utcnow(), source="ecobee", hvac_mode="cool", heat_sp_f=68, cool_sp_f=72, **kw)
    with session_scope() as s:
        s.add(LiveUnit(unit_key=unit_key, ts=snap.ts, source="ecobee", snapshot=snap.model_dump(mode="json")))


def test_manual_hold_gets_what_was_typed_without_a_step_limit(owner):
    _live("up")  # currently cooling to 72°F; the owner wants 80°F (8°F more than max_step_f)
    r = owner.post("/api/control/hold", json={"unit_key": "up", "heat_f": 62, "cool_f": 80})
    assert r.status_code == 200, r.text
    assert (r.json()["request"]["heat_f"], r.json()["request"]["cool_f"]) == (62.0, 80.0)


def test_manual_hold_respects_the_thermostats_own_minimum_gap(owner):
    _live("main", settings={"heatCoolMinDelta": 50})  # ecobee reports tenths: 5°F
    r = owner.post("/api/control/hold", json={"unit_key": "main", "heat_f": 70, "cool_f": 74})
    assert r.status_code == 422 and "at least 5°F below cool (the thermostat's own minimum gap)" in _messages(r)
    assert owner.post("/api/control/hold", json={"unit_key": "main", "heat_f": 69, "cool_f": 74}).status_code == 200
    # without a reported setting the hard limits' deadband (3°F) is the rule
    assert owner.post("/api/control/hold", json={"unit_key": "bed", "heat_f": 70, "cool_f": 74}).status_code == 200


def test_resume_schedule_and_back_to_automatic(owner, agent):
    r = owner.post("/api/control/resume", json={"unit_key": "up"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert (a["action"], a["status"], a["actor"], a["request"]["kind"]) == ("resume_program", "queued", "owner",
                                                                            "resume_schedule")
    assert a["reason"] == "Owner: resume schedule (the ecobee schedule runs; the controller waits 4 h)"
    assert a["request"]["unit_key"] == "up" and a["request"]["reason"] == a["reason"]

    with session_scope() as s:
        put_setting(s, "control", ControlSettings(resume_backoff_hours=1.5))
    assert "waits 1.5 h" in owner.post("/api/control/resume", json={"unit_key": "up"}).json()["reason"]
    with session_scope() as s:
        put_setting(s, "control", ControlSettings(resume_backoff_hours=0))
    assert "steers again at once" in owner.post("/api/control/resume", json={"unit_key": "up"}).json()["reason"]

    r = owner.post("/api/control/automatic", json={"unit_key": "main"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert (a["action"], a["status"], a["request"]["kind"]) == ("resume_program", "queued", "automatic")
    assert a["reason"] == "Owner: back to automatic (the controller steers again now)"

    assert owner.post("/api/control/automatic", json={"unit_key": "attic"}).status_code == 404
    assert agent.post("/api/control/automatic", json={"unit_key": "main"}).status_code == 403
    assert agent.post("/api/control/resume", json={"unit_key": "main"}).status_code == 403
    with session_scope() as s:
        kinds = [r.request["kind"] for r in s.execute(select(ControlAction).order_by(ControlAction.id)).scalars()]
    assert kinds == ["resume_schedule", "resume_schedule", "resume_schedule", "automatic"]


def test_resumes_go_over_homekit_while_the_cloud_is_down(owner):
    """While the ecobee cloud circuit is open and HomeKit has taken over, the owner's resumes are
    queued for the homekit service (channel 'homekit', the owner's kind kept), so a person's
    hold seen during the outage can still be ended; an owner hold stays on the cloud."""
    def circuit(until):
        with session_scope() as s:
            put_setting(s, "source", SourceSettings(kind="ecobee", homekit_enabled=True, cloud_circuit_open_until=until))

    circuit(utcnow() + timedelta(minutes=15))
    auto = owner.post("/api/control/automatic", json={"unit_key": "up"}).json()
    assert (auto["channel"], auto["action"], auto["status"], auto["request"]["kind"]) == (
        "homekit", "resume_program", "queued", "automatic")
    resume = owner.post("/api/control/resume", json={"unit_key": "up"}).json()
    assert (resume["channel"], resume["request"]["kind"]) == ("homekit", "resume_schedule")
    hold = owner.post("/api/control/hold", json={"unit_key": "up", "heat_f": 68, "cool_f": 76}).json()
    assert hold["channel"] == "ecobee"

    circuit(utcnow() - timedelta(minutes=1))  # the circuit has closed: back on the cloud
    assert owner.post("/api/control/automatic", json={"unit_key": "up"}).json()["channel"] == "ecobee"
    with session_scope() as s:  # HomeKit off: nothing can carry it but the cloud
        put_setting(s, "source", SourceSettings(kind="ecobee", homekit_enabled=False,
                                                cloud_circuit_open_until=utcnow() + timedelta(minutes=15)))
    assert owner.post("/api/control/automatic", json={"unit_key": "up"}).json()["channel"] == "ecobee"
    with session_scope() as s:  # never while simulating
        put_setting(s, "source", SourceSettings(kind="simulator", homekit_enabled=True,
                                                cloud_circuit_open_until=utcnow() + timedelta(minutes=15)))
    assert owner.post("/api/control/automatic", json={"unit_key": "up"}).json()["channel"] == "simulator"


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

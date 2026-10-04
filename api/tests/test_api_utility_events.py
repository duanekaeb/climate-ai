"""Utility events routes: the list (order, window, labels, can_skip, prep_label from the plan),
the owner's skip (every thermostat of the event or one) and its undo, and the 409 cases.
Rows are inserted directly; the plan is monkeypatched."""

from __future__ import annotations

from datetime import timedelta

import pytest

from climate.api.schemas import PlanRow
from climate.control import controller
from climate.control.guardrails import GuardResult
from climate.control.policy import UnitTarget
from climate.store.app_settings import ControlSettings, put_setting
from climate.store.db import session_scope
from climate.store.orm import UtilityEvent
from climate.timeutil import utcnow


def add_event(unit_key: str = "up", key: str = "link:peak-1", status: str = "announced", start=None, end=None,
              **kw) -> int:
    now = utcnow()
    start = start if start is not None else now + timedelta(hours=3)
    end = end if end is not None else start + timedelta(hours=3)
    with session_scope() as s:
        row = UtilityEvent(
            unit_key=unit_key, event_key=key, event_type="demandResponse", name=kw.pop("name", "Peak saver"),
            status=status, start_at=start, end_at=end, first_seen_at=kw.pop("first_seen_at", now - timedelta(hours=1)),
            last_seen_at=now, is_relative=kw.pop("is_relative", True), cool_offset_f=kw.pop("cool_offset_f", 2.0),
            is_optional=kw.pop("is_optional", True), detail=kw.pop("detail", {}), **kw,
        )
        s.add(row)
        s.flush()
        return row.id


def get_row(event_id: int) -> UtilityEvent:
    with session_scope() as s:
        row = s.get(UtilityEvent, event_id)
        s.expunge(row)
        return row


def set_mode(mode: str) -> None:
    with session_scope() as s:
        put_setting(s, "control", ControlSettings(mode=mode))


# --- the list --------------------------------------------------------------------------------


def test_list_newest_first_with_labels(owner, agent):
    now = utcnow()
    soon = add_event("up", "link:a")
    running = add_event("main", "link:b", status="running", start=now - timedelta(hours=1),
                        started_at=now - timedelta(hours=1), is_relative=False, cool_offset_f=None, cool_f=78.0)
    mandatory = add_event("bed", "link:c", status="running", start=now - timedelta(minutes=30), is_optional=False,
                          detail={"is_cool_off": True})
    old = add_event("bed", "link:d", status="ended", start=now - timedelta(days=10), ended_at=now - timedelta(days=10))
    ancient = add_event("bed", "link:e", status="opted_out", start=now - timedelta(days=40), skip="done",
                        skip_by="owner", ended_at=now - timedelta(days=40))

    r = agent.get("/api/utility-events")  # a reader route: the agent may look
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [e["id"] for e in rows] == [soon, mandatory, running, old]  # by start, newest first; 40 days ago is out
    by_id = {e["id"]: e for e in rows}
    assert by_id[soon]["unit_name"] == "Upstairs" and by_id[soon]["change_label"] == "cooling +2°F"
    assert by_id[running]["change_label"] == "cooling set to 78°F"
    assert by_id[mandatory]["change_label"] == "AC off"
    assert by_id[soon]["can_skip"] is True and by_id[running]["can_skip"] is True
    assert by_id[mandatory]["can_skip"] is False and by_id[old]["can_skip"] is False
    assert all(e["prep_label"] is None for e in rows)

    longer = owner.get("/api/utility-events", params={"days": 60}).json()
    assert ancient in [e["id"] for e in longer]
    assert owner.get("/api/utility-events", params={"days": 0}).status_code == 422


def test_prep_label_comes_from_the_plan(owner, monkeypatch):
    now = utcnow()
    first = add_event("up", "link:a", start=now + timedelta(hours=1))
    later = add_event("up", "link:b", start=now + timedelta(days=1))
    other = add_event("main", "link:a", start=now + timedelta(hours=1))
    reason = "Pre-cooling 2°F before the utility event at 3:00 PM; this hold ends by 2:50 PM, before the event starts."
    target = UnitTarget(unit_key="up", heat_f=68, cool_f=75, rule="event_prep", reason=reason,
                        hold_end_by=now + timedelta(minutes=50))
    plain = UnitTarget(unit_key="main", heat_f=68, cool_f=76, rule="comfort", reason="Living Room occupied")
    rows = [PlanRow(target=t, guard=GuardResult(ok=True, heat_f=t.heat_f, cool_f=t.cool_f), current_heat_f=68,
                    current_cool_f=77, would_write=False) for t in (target, plain)]
    monkeypatch.setattr(controller, "current_plan", lambda session, now=None: rows)
    by_id = {e["id"]: e for e in owner.get("/api/utility-events").json()}
    assert by_id[first]["prep_label"] == reason
    assert by_id[later]["prep_label"] is None and by_id[other]["prep_label"] is None

    # undoing a skip answers with the event as the plan sees it: the prep is for the earliest one
    assert owner.post(f"/api/utility-events/{later}/skip", json={"all_units": False}).json()["prep_label"] is None
    assert owner.post(f"/api/utility-events/{later}/unskip").json()["prep_label"] is None
    assert owner.post(f"/api/utility-events/{first}/skip", json={"all_units": False}).json()["prep_label"] is None
    assert owner.post(f"/api/utility-events/{first}/unskip").json()["prep_label"] == reason


# --- skip ------------------------------------------------------------------------------------


def test_skip_every_thermostat_of_the_event(owner):
    up = add_event("up", "link:a")
    main = add_event("main", "link:a")
    bed = add_event("bed", "link:a", is_optional=False)  # mandatory there: left alone
    other = add_event("bed", "link:z")
    r = owner.post(f"/api/utility-events/{up}/skip")  # no body: all_units by default
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["skip"], body["skip_by"], body["skip_reason"]) == (up, "requested", "owner",
                                                                                  "Skipped from the app")
    assert body["skip_requested_at"] is not None and body["can_skip"] is False
    assert get_row(main).skip == "requested" and get_row(main).skip_by == "owner"
    assert get_row(bed).skip is None and get_row(other).skip is None


def test_skip_only_this_thermostat(owner):
    up = add_event("up", "link:a")
    main = add_event("main", "link:a")
    r = owner.post(f"/api/utility-events/{up}/skip", json={"all_units": False})
    assert r.status_code == 200, r.text
    assert get_row(up).skip == "requested" and get_row(main).skip is None


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"is_optional": False}, "mandatory"),
        ({"skip": "refused", "skip_by": "owner"}, "mandatory"),
        ({"status": "ended"}, "over"),
        ({"status": "running", "start": "past", "end": "past"}, "over"),
        ({"skip": "requested", "skip_by": "rule"}, "already waiting"),
        ({"status": "opted_out", "skip": "done", "skip_by": "owner"}, "over"),
        ({"status": "running", "skip": "done", "skip_by": "owner"}, "already skipped"),
    ],
)
def test_skip_conflicts(owner, kw, needle):
    now = utcnow()
    if kw.get("start") == "past":
        kw = {**kw, "start": now - timedelta(hours=3), "end": now - timedelta(minutes=1)}
    event_id = add_event("up", "link:a", **kw)
    before = get_row(event_id)
    r = owner.post(f"/api/utility-events/{event_id}/skip")
    assert r.status_code == 409, r.text
    assert needle in r.json()["detail"]
    after = get_row(event_id)
    assert (after.skip, after.skip_by, after.skip_requested_at) == (before.skip, before.skip_by, before.skip_requested_at)


def test_skip_needs_the_controller_on_and_the_owner(owner, agent):
    event_id = add_event("up", "link:a")
    set_mode("off")
    r = owner.post(f"/api/utility-events/{event_id}/skip")
    assert r.status_code == 409 and "controller is off" in r.json()["detail"]
    set_mode("act")
    assert agent.post(f"/api/utility-events/{event_id}/skip", json={}).status_code == 403
    assert get_row(event_id).skip is None
    assert owner.post("/api/utility-events/999999/skip").status_code == 404
    assert owner.post(f"/api/utility-events/{event_id}/skip").status_code == 200


def test_a_failed_skip_can_be_requested_again_with_fresh_attempts(owner):
    event_id = add_event("up", "link:a", status="running", start=utcnow() - timedelta(minutes=20), skip="failed",
                         skip_by="owner", detail={"skip_attempts": 3, "name": "Peak saver"})
    r = owner.post(f"/api/utility-events/{event_id}/skip")
    assert r.status_code == 200, r.text
    row = get_row(event_id)
    assert row.skip == "requested" and row.detail == {"skip_attempts": 0, "name": "Peak saver"}


# --- unskip ----------------------------------------------------------------------------------


def test_unskip_every_thermostat_or_one(owner):
    up = add_event("up", "link:a")
    main = add_event("main", "link:a")
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    r = owner.post(f"/api/utility-events/{main}/unskip")
    assert r.status_code == 200, r.text
    assert r.json()["skip"] is None and r.json()["can_skip"] is True
    for event_id in (up, main):
        row = get_row(event_id)
        assert (row.skip, row.skip_by, row.skip_reason, row.skip_requested_at) == (None, None, None, None)

    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    assert owner.post(f"/api/utility-events/{up}/unskip", json={"all_units": False}).status_code == 200
    assert get_row(up).skip is None and get_row(main).skip == "requested"


def test_unskip_conflicts(owner, agent):
    nothing = add_event("up", "link:a")
    r = owner.post(f"/api/utility-events/{nothing}/unskip")
    assert r.status_code == 409 and "No skip" in r.json()["detail"]
    done = add_event("main", "link:b", status="opted_out", skip="done", skip_by="owner")
    r = owner.post(f"/api/utility-events/{done}/unskip")
    assert r.status_code == 409 and "already recorded" in r.json()["detail"]
    assert get_row(done).skip == "done"
    pending = add_event("bed", "link:c", skip="requested", skip_by="rule", skip_reason="Girls' Room reached 79.5°F")
    assert agent.post(f"/api/utility-events/{pending}/unskip", json={}).status_code == 403
    assert owner.post("/api/utility-events/999999/unskip").status_code == 404
    assert owner.post(f"/api/utility-events/{pending}/unskip").status_code == 200  # the owner's undo beats a rule
    assert get_row(pending).skip is None

"""Utility events routes: the list (order, window, labels, can_skip, prep_label from the plan
and what the controller actually does about it), the owner's skip (every thermostat of the
event or one) and its undo (refused once the opt-out is being sent; it forgets failed
attempts and resolves the "skip waiting" alerts), and the 409 cases. Rows are inserted
directly; the plan is monkeypatched except in the prep-label cases that run the real one."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from climate.api.schemas import PlanRow
from climate.control import controller
from climate.control.guardrails import GuardResult
from climate.control.policy import UnitTarget
from climate.sources.base import HoldInfo, UnitSnapshot, WriteResult
from climate.store.app_settings import ControlSettings, UtilityEventSettings, put_setting
from climate.store.db import session_scope
from climate.store.orm import Alert, ControlAction, LiveUnit, UtilityEvent
from climate.timeutil import utcnow
from climate.utility import events, skips


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
                    current_cool_f=77, would_write=t.rule == "event_prep") for t in (target, plain)]
    monkeypatch.setattr(controller, "current_plan", lambda session, now=None: rows)
    set_mode("act")  # the controller writes the prep hold: the plan's sentence is a fact
    by_id = {e["id"]: e for e in owner.get("/api/utility-events").json()}
    assert by_id[first]["prep_label"] == reason
    assert by_id[later]["prep_label"] is None and by_id[other]["prep_label"] is None

    # undoing a skip answers with the event as the plan sees it: the prep is for the earliest one
    assert owner.post(f"/api/utility-events/{later}/skip", json={"all_units": False}).json()["prep_label"] is None
    assert owner.post(f"/api/utility-events/{later}/unskip").json()["prep_label"] is None
    assert owner.post(f"/api/utility-events/{first}/skip", json={"all_units": False}).json()["prep_label"] is None
    assert owner.post(f"/api/utility-events/{first}/unskip").json()["prep_label"] == reason


def _house_with_an_event(mode: str, *, hold: str | None = None, act_units: list[str] | None = None) -> None:
    """Fresh simulator snapshots, pre-cooling on, and an event on Upstairs in 2 h: the real plan
    gives Upstairs rule 'event_prep'. ``hold``: 'person' (somebody set 72°F at the wall)."""
    now = utcnow()
    with session_scope() as s:
        put_setting(s, "control", ControlSettings(mode=mode, act_units=act_units or ["main", "up", "bed"]))
        put_setting(s, "utility_events", UtilityEventSettings(precondition=True))
        for k in ("main", "up", "bed"):
            held = None
            if k == "up" and hold == "person":
                held = HoldInfo(heat_f=66, cool_f=72, start=now - timedelta(minutes=20), end=now + timedelta(hours=4),
                                hold_type="holdHours")
            snap = UnitSnapshot(unit_key=k, ts=now, source="simulator", hvac_mode="cool", heat_sp_f=68, cool_sp_f=76,
                                zone_temp_f=75.5, hold=held)
            s.add(LiveUnit(unit_key=k, ts=now, source="simulator", snapshot=snap.model_dump(mode="json")))
    add_event("up", "link:peak", start=now + timedelta(hours=2))


def _labels(client) -> tuple[str | None, str | None, dict]:
    """(the Live card's prep_label, the list's, the plan row for Upstairs)."""
    up = next(u for u in client.get("/api/status").json()["units"] if u["unit_key"] == "up")
    listed = client.get("/api/utility-events").json()
    row = next(r for r in client.get("/api/control/plan").json()["rows"] if r["target"]["unit_key"] == "up")
    assert row["target"]["rule"] == "event_prep"
    return up["utility_event"]["prep_label"], listed[0]["prep_label"], row


@pytest.mark.parametrize(
    ("mode", "hold", "act_units", "expect"),
    [
        ("act", None, None, "fact"),
        ("suggest", None, None, "Suggest mode: would pre-cool 2°F before the utility event at "),
        ("act", None, ["main"], "Upstairs is suggest-only: would pre-cool 2°F before the utility event at "),
        ("off", None, None, None),
        ("act", "person", None, None),  # a person's hold wins: nothing is written, nothing to claim
        ("suggest", "person", None, None),
    ],
)
def test_prep_label_says_what_the_controller_does(owner, mode, hold, act_units, expect):
    _house_with_an_event(mode, hold=hold, act_units=act_units)
    card, listed, row = _labels(owner)
    assert card == listed
    if expect is None:
        assert card is None
    elif expect == "fact":
        assert row["would_write"] is True and card == row["target"]["reason"]
        assert card.startswith("Pre-cooling 2°F before the utility event at ")
    else:
        assert card.startswith(expect) and ", with a hold ending by " in card
        assert card.endswith(". Nothing is written.")


def test_prep_label_while_our_prep_hold_runs(owner):
    """Our prep hold, written 10 minutes ago, is running: the rate limit blocks the guard and
    nothing is about to be written, yet the house IS being pre-cooled."""
    _house_with_an_event("act")
    _, _, row = _labels(owner)
    heat, cool = row["guard"]["heat_f"], row["guard"]["cool_f"]
    now = utcnow()
    written = now - timedelta(minutes=10)
    assert written + timedelta(hours=1) <= datetime.fromisoformat(row["target"]["hold_end_by"])
    with session_scope() as s:
        live = s.get(LiveUnit, "up")
        hold = HoldInfo(heat_f=heat, cool_f=cool, start=written, end=written + timedelta(hours=1),
                        hold_type="holdHours", set_by_us=True)
        snap = UnitSnapshot(unit_key="up", ts=now, source="simulator", hvac_mode="cool", heat_sp_f=heat,
                            cool_sp_f=cool, zone_temp_f=75.5, hold=hold)
        live.ts, live.snapshot = now, snap.model_dump(mode="json")
        s.add(ControlAction(ts=written, unit_key="up", actor="controller", mode="act", channel="simulator",
                            action="set_hold", status="verified", rule="event_prep", reason="prep",
                            request={"unit_key": "up", "heat_f": heat, "cool_f": cool, "hours": 1,
                                     "hold_end_by": row["target"]["hold_end_by"]}, completed_at=written))
    card, listed, row = _labels(owner)
    assert row["would_write"] is False and row["guard"]["blocked_reason"]  # the rate limit
    assert card == listed == row["target"]["reason"]


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


def _sent_opt_out(event_id: int, minutes_ago: float = 0.5, status: str = "sent") -> None:
    """The worker's opt_out_event row for this event: 'sent' while its call is on the way."""
    with session_scope() as s:
        s.add(ControlAction(ts=utcnow() - timedelta(minutes=minutes_ago), unit_key=get_row(event_id).unit_key,
                            actor="owner", mode="act", channel="ecobee", action=skips.ACTION, status=status,
                            rule=skips.RULE, reason="Skipped from the app: opting out",
                            request={"kind": skips.REQUEST_KIND, "event_id": event_id, "by": "owner"}))


def test_unskip_refused_once_the_opt_out_is_being_sent(owner):
    up = add_event("up", "link:a", status="running", start=utcnow() - timedelta(minutes=20))
    main = add_event("main", "link:a", status="running", start=utcnow() - timedelta(minutes=20))
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    _sent_opt_out(up)
    r = owner.post(f"/api/utility-events/{up}/unskip")
    assert r.status_code == 409
    assert r.json()["detail"] == "The opt-out is already being sent to ecobee; it can't be taken back."
    assert get_row(up).skip == "requested" and get_row(main).skip == "requested"

    # the other thermostat's undo takes back only what is not on its way
    r = owner.post(f"/api/utility-events/{main}/unskip")
    assert r.status_code == 200, r.text
    assert get_row(main).skip is None and get_row(up).skip == "requested" and get_row(up).skip_by == "owner"


def test_a_stale_or_finished_send_does_not_block_the_undo(owner):
    up = add_event("up", "link:a", status="running", start=utcnow() - timedelta(minutes=40))
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    _sent_opt_out(up, minutes_ago=20)  # the worker died mid-call long ago
    _sent_opt_out(up, minutes_ago=5, status="failed")  # an attempt that failed: retried later
    assert owner.post(f"/api/utility-events/{up}/unskip").status_code == 200
    assert get_row(up).skip is None


async def test_undo_during_the_call_is_refused_and_the_skip_recorded(owner):
    """The owner taps Undo while the worker's call is on its way: 409, and the skip ends 'done'
    with who asked for it kept (never a 200 for an opt-out ecobee records anyway)."""
    up = add_event("up", "link:a", status="running", start=utcnow() - timedelta(minutes=20),
                   detail={"link_ref": "a", "name": "Peak saver"})
    with session_scope() as s:
        put_setting(s, "control", ControlSettings(mode="act"))
        top = HoldInfo(hold_type="demandResponse", event_name="Peak saver", link_ref="a")
        snap = UnitSnapshot(unit_key="up", ts=utcnow(), source="ecobee", hvac_mode="cool", heat_sp_f=68,
                            cool_sp_f=78, hold=top,
                            events=[{"event_type": "demandResponse", "name": "Peak saver", "running": True,
                                     "link_ref": "a"}])
        s.add(LiveUnit(unit_key="up", ts=snap.ts, source="ecobee", snapshot=snap.model_dump(mode="json")))
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    answers: list[int] = []

    class UndoMidCall:
        kind = "ecobee"

        async def opt_out_event(self, unit_key, reason, **identity):
            answers.append(owner.post(f"/api/utility-events/{up}/unskip").status_code)
            return WriteResult(ok=True, channel="ecobee", readback={"event_running": False},
                               request={"function": {"type": "resumeProgram"}, "sent": True})

    assert len(await skips.run(UndoMidCall())) == 1
    row = get_row(up)
    assert answers == [409] and (row.skip, row.status, row.skip_by) == ("done", "opted_out", "owner")


def test_undo_forgets_failed_attempts_so_a_new_skip_gets_them_all(owner):
    up = add_event("up", "link:a", status="running", start=utcnow() - timedelta(minutes=20),
                   detail={"name": "Peak saver", "rule_requested_at": "2026-07-14T20:00:00+00:00"})
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    with session_scope() as s:  # two attempts failed meanwhile
        row = s.get(UtilityEvent, up)
        row.detail = {**row.detail, "skip_attempts": 2}
    assert owner.post(f"/api/utility-events/{up}/unskip").status_code == 200
    # a skip rule still asks only once; the count is gone
    assert get_row(up).detail == {"name": "Peak saver", "rule_requested_at": "2026-07-14T20:00:00+00:00"}
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    assert (get_row(up).detail or {}).get("skip_attempts", 0) == 0


def test_undo_resolves_the_skip_waiting_alerts(owner):
    up = add_event("up", "link:a", status="running", start=utcnow() - timedelta(minutes=20))
    main = add_event("main", "link:a", status="running", start=utcnow() - timedelta(minutes=20))
    assert owner.post(f"/api/utility-events/{up}/skip").status_code == 200
    with session_scope() as s:
        for phase in ("skip_waiting_off", "skip_waiting_cloud"):
            s.add(Alert(level="info", kind=events.ALERT_KIND, title="Skip waiting", body="",
                        dedupe_key=events.alert_key("link:a", phase)))

    def open_waits() -> int:
        with session_scope() as s:
            return len(s.execute(select(Alert.id).where(Alert.resolved_at.is_(None))).all())

    assert owner.post(f"/api/utility-events/{up}/unskip", json={"all_units": False}).status_code == 200
    assert open_waits() == 2  # Main floor's skip still waits
    assert owner.post(f"/api/utility-events/{main}/unskip").status_code == 200
    assert open_waits() == 0

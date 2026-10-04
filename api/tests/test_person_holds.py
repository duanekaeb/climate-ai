"""A hold a person set always wins (spec "holds and utility events" §1): timed and indefinite
person holds, Resume at the thermostat (back-off from first seen, full length), holds that end
on their own, ours vs a person's by window (bug 3), the owner's two resumes, owner holds, the
reminder alert, HomeKit's view, and the sensor-set pause (bug 6)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from climate.control import controller
from climate.sources.base import HoldInfo
from climate.state import MANUAL_KIND, RESUME_KIND, load_house_state
from climate.store.app_settings import put_setting
from climate.store.orm import Alert, ControlAction, LiveUnit, UtilityEvent
from tests.test_controller_safety import (
    DAY_SETS,
    NIGHT,
    SetsSource,
    our_hold,
    put_homekit_snapshot,
    snapshots_with_sets,
)
from tests.test_controller_tick import NOW, FakeSource, actions, put_snapshot, refresh_sensors, setup_house

INDEFINITE_END = datetime(2035, 1, 1, tzinfo=NOW.tzinfo)


def at(db, t: datetime, unit: str = "up", **snap) -> None:
    """A fresh snapshot of ``unit`` one minute before ``t`` (others refreshed too), sensors fresh."""
    for u in ("main", "up", "bed"):
        if u == unit:
            put_snapshot(db, u, t - timedelta(minutes=1), **snap)
        else:
            put_snapshot(db, u, t - timedelta(minutes=1))
    refresh_sensors(db, t)


def person(heat: float = 70.0, cool: float = 74.0, **kw) -> HoldInfo:
    return HoldInfo(heat_f=heat, cool_f=cool, **kw)


def skipped(db, kind: str, unit: str = "up") -> list[ControlAction]:
    return [r for r in actions(db, unit_key=unit, status="skipped") if (r.request or {}).get("kind") == kind]


def owner_resume(db, kind: str | None, ts: datetime, unit: str = "up", status: str = "verified") -> None:
    request = {"unit_key": unit} if kind is None else {"unit_key": unit, "kind": kind}
    db.add(ControlAction(ts=ts, completed_at=ts + timedelta(minutes=1), unit_key=unit, actor="owner", mode="act",
                         channel="simulator", action="resume_program", status=status, rule="manual",
                         reason="Owner resume", request=request))
    db.commit()


def utility_event(db, unit: str, start: datetime, end: datetime, status: str = "running", **kw) -> int:
    row = UtilityEvent(unit_key=unit, event_key=f"link:{unit}-{start:%H%M}", name="Peak saver", status=status,
                       start_at=start, end_at=end, first_seen_at=start - timedelta(hours=1), last_seen_at=start,
                       is_relative=True, cool_offset_f=2.0, is_optional=True, detail={}, **kw)
    db.add(row)
    db.commit()
    return row.id


# ---------------------------------------------------------------------------------------
# person holds win, timed and indefinite
# ---------------------------------------------------------------------------------------


async def test_timed_person_hold_wins_until_it_ends_then_the_controller_takes_over(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=10), end=NOW + timedelta(hours=2), hold_type="holdHours")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.holds == []
    (row,) = skipped(db, MANUAL_KIND)
    assert row.reason == ("Someone set a hold on the upstairs thermostat (70–74°F, until 4:00 PM); the controller "
                          "waits until it ends or you choose Back to automatic.")
    assert row.request["until"] == (NOW + timedelta(hours=2)).isoformat() and row.request["by"] == "thermostat"
    ph = load_house_state(db, NOW).units["up"].person_hold
    assert (ph.since, ph.until, ph.detection_id, ph.by) == (NOW - timedelta(minutes=10), NOW + timedelta(hours=2),
                                                             row.id, "thermostat")
    plan = {r.target.unit_key: r for r in controller.current_plan(db, NOW)}["up"]
    assert plan.guard.blocked_reason == ("On your hold since 1:50 PM, until 4:00 PM; the controller waits until it "
                                         "ends or you choose Back to automatic.")
    assert not plan.would_write

    t = NOW + timedelta(minutes=110)  # still running: nothing written, logged once
    at(db, t, heat=70.0, cool=74.0, hold=hold)
    await controller.tick(src, t)
    assert src.holds == [] and len(skipped(db, MANUAL_KIND)) == 1

    t = NOW + timedelta(hours=2, minutes=2)  # it ended on its own: the controller takes over at once
    at(db, t, heat=67.0, cool=78.0)
    await controller.tick(src, t)
    assert [(h.heat_f, h.cool_f) for h in src.holds] == [(68.0, 77.0)]
    assert skipped(db, RESUME_KIND) == []
    unit = load_house_state(db, t).units["up"]
    assert unit.person_hold is None and unit.resume_backoff_until is None


async def test_a_timed_hold_past_its_end_is_over_even_while_the_snapshot_lags(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(hours=1), end=NOW - timedelta(minutes=1), hold_type="holdHours")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    assert load_house_state(db, NOW).units["up"].person_hold is None
    src = FakeSource()
    await controller.tick(src, NOW)
    assert len(src.holds) == 1 and skipped(db, MANUAL_KIND) == []


async def test_indefinite_person_hold_wins_for_days(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=5), end=INDEFINITE_END, hold_type="indefinite")
    src = FakeSource()
    for t in (NOW, NOW + timedelta(hours=5), NOW + timedelta(days=1), NOW + timedelta(days=3)):
        at(db, t, heat=70.0, cool=74.0, hold=hold)
        await controller.tick(src, t)
        unit = load_house_state(db, t).units["up"]
        assert unit.person_hold is not None and unit.person_hold.until is None
        assert unit.person_hold.since == NOW - timedelta(minutes=5)
    assert src.holds == [] and src.resumes == []
    (row,) = skipped(db, MANUAL_KIND)
    assert "until you change it" in row.reason and row.request["until"] is None


async def test_a_first_sighting_long_after_the_hold_started_changes_nothing(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(hours=26), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    ph = load_house_state(db, NOW).units["up"].person_hold
    assert ph.since == NOW - timedelta(hours=26) and ph.first_seen == NOW and ph.until is None
    assert src.holds == []
    assert "since Tue 12:00 PM" in controller.current_plan(db, NOW)[1].guard.blocked_reason


# ---------------------------------------------------------------------------------------
# Resume, natural ends
# ---------------------------------------------------------------------------------------


async def test_resume_on_a_persons_hold_backs_off_from_first_seen_for_the_full_length(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    (det,) = skipped(db, MANUAL_KIND)

    t1 = NOW + timedelta(minutes=20)  # Resume pressed at the thermostat
    at(db, t1)
    await controller.tick(src, t1)
    (row,) = skipped(db, RESUME_KIND)
    assert row.request == {"kind": RESUME_KIND, "person_hold_detection_id": det.id}
    assert row.reason == ("Someone pressed Resume at the upstairs thermostat; following the ecobee schedule until "
                          "6:20 PM.")
    unit = load_house_state(db, t1).units["up"]
    assert (unit.resume_backoff_until, unit.resume_seen_at, unit.person_hold) == (t1 + timedelta(hours=4), t1, None)
    assert src.holds == []

    t2 = t1 + timedelta(hours=3, minutes=50)  # logged once, still backing off
    at(db, t2)
    await controller.tick(src, t2)
    assert src.holds == [] and len(skipped(db, RESUME_KIND)) == 1
    assert "follows the ecobee schedule until 6:20 PM" in controller.current_plan(db, t2)[1].guard.blocked_reason

    t3 = t1 + timedelta(hours=4, minutes=1)
    at(db, t3)
    await controller.tick(src, t3)
    assert len(src.holds) == 1


async def test_resume_seen_late_backs_off_from_when_it_was_first_seen(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    await controller.tick(FakeSource(), NOW)
    later = NOW + timedelta(days=2)  # the server was down; the hold is gone by now
    at(db, later)
    await controller.tick(FakeSource(), later)
    unit = load_house_state(db, later).units["up"]
    assert unit.resume_seen_at == later and unit.resume_backoff_until == later + timedelta(hours=4)


@pytest.mark.parametrize("vanished", [timedelta(minutes=0), timedelta(minutes=-4), timedelta(minutes=30)])
async def test_a_persons_hold_that_ended_on_its_own_starts_no_backoff(db, vanished):
    setup_house(db, act_units=["up"])
    end = NOW + timedelta(hours=1)
    hold = person(start=NOW - timedelta(minutes=10), end=end, hold_type="holdHours")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    t = end + vanished + timedelta(minutes=1)  # the first snapshot without it: at/after its end (± 5 min)
    at(db, t)
    await controller.tick(src, t)
    assert skipped(db, RESUME_KIND) == [] and load_house_state(db, t).units["up"].resume_backoff_until is None
    assert len(src.holds) == 1


async def test_a_persons_timed_hold_resumed_early_backs_off(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=10), end=NOW + timedelta(hours=2), hold_type="holdHours")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    t = NOW + timedelta(minutes=30)
    at(db, t)
    await controller.tick(src, t)
    assert len(skipped(db, RESUME_KIND)) == 1 and src.holds == []
    assert load_house_state(db, t).units["up"].resume_backoff_until == t + timedelta(hours=4)


async def test_our_hold_cancelled_early_backs_off_its_full_length_after_our_old_end(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=100), 68.0, 77.0)  # ends at NOW + 20 min
    at(db, NOW, heat=67.0, cool=78.0)  # Resume pressed: our hold is gone early
    src = FakeSource()
    await controller.tick(src, NOW)
    (row,) = skipped(db, RESUME_KIND)
    assert row.request["cancelled_action_id"] and "following the ecobee schedule until 6:00 PM" in row.reason
    t = NOW + timedelta(hours=2)  # long after our old hold would have ended
    at(db, t)
    await controller.tick(src, t)
    assert src.holds == [] and load_house_state(db, t).units["up"].resume_backoff_until == NOW + timedelta(hours=4)


async def test_two_identical_holds_one_after_the_other_are_two_holds(db):
    setup_house(db, act_units=["up"])
    control = load_house_state(db, NOW).control
    control.manual_hold_reminder_hours = 0.5
    put_setting(db, "control", control)
    db.commit()
    first = person(start=NOW - timedelta(minutes=40), end=NOW + timedelta(minutes=1), hold_type="holdHours")
    at(db, NOW, heat=70.0, cool=74.0, hold=first)
    src = FakeSource()
    await controller.tick(src, NOW)
    t = NOW + timedelta(minutes=3)
    second = person(start=NOW + timedelta(minutes=2), end=NOW + timedelta(hours=1), hold_type="holdHours")
    at(db, t, heat=70.0, cool=74.0, hold=second)
    await controller.tick(src, t)
    d1, d2 = skipped(db, MANUAL_KIND)
    assert d1.request["start"] != d2.request["start"]
    assert load_house_state(db, t).units["up"].person_hold.detection_id == d2.id
    assert src.holds == []
    # the reminder for the first hold is resolved; the second gets its own (once it has run 30 min)
    reminders = list(db.execute(select(Alert).where(Alert.kind == "person_hold").order_by(Alert.id)).scalars())
    assert [(a.dedupe_key, a.resolved_at is not None) for a in reminders] == [(f"person_hold:up:{d1.id}", True)]


# ---------------------------------------------------------------------------------------
# ours vs a person's (bug 3)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("start", "end"), [
    (timedelta(minutes=-5), timedelta(hours=3)),  # ends long after our hold would
    (timedelta(hours=-2), timedelta(minutes=80)),  # started before our write
    (timedelta(minutes=-5), INDEFINITE_END),  # "until I change it"
])
async def test_our_setpoints_with_another_window_are_a_persons_hold(db, start, end):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=30), 68.0, 77.0)
    end_at = end if isinstance(end, datetime) else NOW + end
    at(db, NOW, heat=68.0, cool=77.0, hold=person(68.0, 77.0, start=NOW + start, end=end_at))
    unit = load_house_state(db, NOW).units["up"]
    assert unit.person_hold is not None and unit.snapshot.hold.set_by_us is False
    await controller.tick(FakeSource(), NOW)
    assert len(skipped(db, MANUAL_KIND)) == 1


async def test_our_setpoints_and_window_are_ours(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=30), 68.0, 77.0)
    at(db, NOW, heat=68.0, cool=77.0,
       hold=person(68.0, 77.0, start=NOW - timedelta(minutes=30), end=NOW + timedelta(minutes=90)))
    unit = load_house_state(db, NOW).units["up"]
    assert unit.person_hold is None and unit.snapshot.hold.set_by_us is True


# ---------------------------------------------------------------------------------------
# the owner's resumes and holds
# ---------------------------------------------------------------------------------------


async def test_resume_schedule_backs_off_and_back_to_automatic_cancels_it(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    owner_resume(db, "resume_schedule", NOW + timedelta(minutes=1))  # completed at NOW + 2 min
    t = NOW + timedelta(minutes=5)
    at(db, t)
    await controller.tick(src, t)
    unit = load_house_state(db, t).units["up"]
    assert unit.resume_backoff_until == NOW + timedelta(minutes=2, hours=4)
    assert skipped(db, RESUME_KIND) == []  # the owner's own resume is not "someone pressed Resume"
    assert src.holds == []

    owner_resume(db, "automatic", NOW + timedelta(minutes=10))
    t = NOW + timedelta(minutes=40)
    at(db, t)
    unit = load_house_state(db, t).units["up"]
    assert unit.resume_backoff_until is None
    assert unit.last_change_at == NOW + timedelta(minutes=1)  # "automatic" never counts as a change
    await controller.tick(src, t)
    assert len(src.holds) == 1


async def test_back_to_automatic_lets_the_controller_steer_at_once(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    src = FakeSource()
    await controller.tick(src, NOW)
    owner_resume(db, "automatic", NOW + timedelta(minutes=1))
    t = NOW + timedelta(minutes=3)
    at(db, t)
    await controller.tick(src, t)
    assert len(src.holds) == 1 and skipped(db, RESUME_KIND) == []


async def test_an_older_owner_resume_with_no_kind_counts_as_resume_schedule(db):
    setup_house(db, act_units=["up"])
    owner_resume(db, None, NOW - timedelta(minutes=10))
    assert load_house_state(db, NOW).units["up"].resume_backoff_until == NOW - timedelta(minutes=9) + timedelta(hours=4)
    owner_resume(db, "resume_schedule", NOW - timedelta(minutes=8), status="failed")  # never verified: no effect
    assert load_house_state(db, NOW).units["up"].resume_backoff_until == NOW - timedelta(minutes=9) + timedelta(hours=4)


async def test_owner_hold_gets_exactly_what_was_typed_within_the_hard_envelope(db):
    setup_house(db, mode="suggest")
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=66.0, cool=74.0)
    live = db.get(LiveUnit, "up")
    live.snapshot = {**live.snapshot, "zone_humidity": 70.0}  # humid: the controller would not raise cooling
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    put_snapshot(db, "bed", NOW - timedelta(minutes=1), heat=70.0, cool=74.0, hold=hold)
    db.add_all([
        # a controller write 5 min ago would rate-limit the controller; not the owner
        ControlAction(ts=NOW - timedelta(minutes=5), unit_key="up", actor="controller", mode="act",
                      channel="simulator", action="set_hold", status="verified", reason="x",
                      request={"heat_f": 66.0, "cool_f": 74.0}),
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="up", actor="owner", mode="act", channel="simulator",
                      action="set_hold", status="queued", rule="manual", reason="Owner hold",
                      request={"unit_key": "up", "heat_f": 69.0, "cool_f": 80.0, "hours": 2}),
        # over a person's hold on the wing: the owner is the person
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="bed", actor="owner", mode="act", channel="simulator",
                      action="set_hold", status="queued", rule="manual", reason="Owner hold",
                      request={"unit_key": "bed", "heat_f": 71.0, "cool_f": 72.0, "hours": 1}),
    ])
    db.commit()
    src = FakeSource()
    await controller.execute_queued(src, NOW)
    # no 2°F step and no humidity guard; the deadband still holds (cool 72 keeps heat at 69)
    assert [(h.unit_key, h.heat_f, h.cool_f) for h in src.holds] == [("up", 69.0, 80.0), ("bed", 69.0, 72.0)]
    up = actions(db, actor="owner", unit_key="up")[0]
    assert up.status == "verified" and "requested" not in up.request
    bed = actions(db, actor="owner", unit_key="bed")[0]
    assert bed.request["requested"] == {"heat_f": 71.0, "cool_f": 72.0} and bed.request["guard"]


async def test_owner_hold_is_blocked_while_a_utility_event_runs_by_the_clock(db):
    setup_house(db, mode="act")
    utility_event(db, "up", NOW - timedelta(minutes=10), NOW + timedelta(hours=2))  # snapshot shows nothing yet
    db.add(ControlAction(ts=NOW - timedelta(minutes=1), unit_key="up", actor="owner", mode="act",
                         channel="simulator", action="set_hold", status="queued", rule="manual", reason="Owner hold",
                         request={"unit_key": "up", "heat_f": 68.0, "cool_f": 75.0, "hours": 1}))
    db.commit()
    src = FakeSource()
    await controller.execute_queued(src, NOW)
    owner = actions(db, actor="owner")[0]
    assert owner.status == "failed" and "Peak saver" in owner.error and owner.error.endswith("Skip the event first.")
    await controller.tick(src, NOW)
    assert [h.unit_key for h in src.holds] == ["main", "bed"]  # the controller stands aside upstairs too


# ---------------------------------------------------------------------------------------
# messages, events, reminders, HomeKit, sensor sets
# ---------------------------------------------------------------------------------------


def test_evaluate_says_why_it_waits_instead_of_the_schedule_matches(db):
    setup_house(db, act_units=["up"])
    settings = {"program_heat_f": 68.0, "program_cool_f": 77.0}  # the schedule matches the plan
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold, settings=settings)
    state = load_house_state(db, NOW)
    target = next(t for t in controller.plan(state) if t.unit_key == "up")
    assert target.desired == "program"
    ev = controller._evaluate(state, state.units["up"], target, NOW, "act", None)
    assert ev.kind == "none" and ev.note.startswith("On your hold since 1:30 PM (until you change it)")

    at(db, NOW, heat=67.0, cool=78.0, hold=None, settings=settings)
    utility_event(db, "up", NOW - timedelta(minutes=5), NOW + timedelta(hours=1))
    state = load_house_state(db, NOW)
    target = next(t for t in controller.plan(state) if t.unit_key == "up")
    ev = controller._evaluate(state, state.units["up"], target, NOW, "act", None)
    assert ev.kind == "none" and "utility event" in ev.note and "schedule already matches" not in ev.note


async def test_unrecognised_ecobee_event_is_hands_off(db):
    setup_house(db, act_units=["up"])
    at(db, NOW, heat=66.0, cool=80.0, hold=HoldInfo(heat_f=66.0, cool_f=80.0, hold_type="today"))
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.holds == [] and actions(db, unit_key="up", status="skipped") == []
    assert load_house_state(db, NOW).units["up"].person_hold is None
    assert "unrecognised ecobee event (today)" in controller.current_plan(db, NOW)[1].guard.blocked_reason


async def test_reminder_once_per_hold_and_resolved_when_it_is_gone(db):
    setup_house(db, act_units=["up"])
    control = load_house_state(db, NOW).control
    control.manual_hold_reminder_hours = 1.0
    put_setting(db, "control", control)
    db.commit()
    hold = person(start=NOW - timedelta(minutes=30), end=INDEFINITE_END, hold_type="indefinite")
    src = FakeSource()

    def reminders() -> list[Alert]:
        db.expire_all()
        return list(db.execute(select(Alert).where(Alert.kind == "person_hold").order_by(Alert.id)).scalars())

    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    await controller.tick(src, NOW)
    assert reminders() == []  # 30 min so far
    for minutes in (31, 40):
        t = NOW + timedelta(minutes=minutes)
        at(db, t, heat=70.0, cool=74.0, hold=hold)
        await controller.tick(src, t)
    (alert,) = reminders()
    (det,) = skipped(db, MANUAL_KIND)
    assert alert.level == "info" and alert.title == "Upstairs is still on your hold"
    assert alert.dedupe_key == f"person_hold:up:{det.id}" and alert.resolved_at is None
    assert "since 1:30 PM (until you change it)" in alert.body and "Back to automatic" in alert.body
    assert src.holds == []  # a reminder never changes anything

    t = NOW + timedelta(minutes=50)  # Resume pressed: the hold is gone, so is the reminder
    at(db, t)
    await controller.tick(src, t)
    (alert,) = reminders()
    assert alert.resolved_at is not None


async def test_reminder_is_not_repeated_after_the_owner_closes_it(db):
    setup_house(db, act_units=["up"])
    control = load_house_state(db, NOW).control
    control.manual_hold_reminder_hours = 0.5
    put_setting(db, "control", control)
    db.commit()
    hold = person(start=NOW - timedelta(hours=1), end=INDEFINITE_END, hold_type="indefinite")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    await controller.tick(FakeSource(), NOW)
    db.execute(update(Alert).values(resolved_at=NOW))
    db.commit()
    t = NOW + timedelta(minutes=5)
    at(db, t, heat=70.0, cool=74.0, hold=hold)
    await controller.tick(FakeSource(), t)
    assert len(db.execute(select(Alert).where(Alert.kind == "person_hold")).scalars().all()) == 1


def homekit_hand_change(db, ts: datetime, unit: str = "up") -> ControlAction:
    row = ControlAction(ts=ts, unit_key=unit, actor="homekit_service", mode="act", channel="homekit",
                        action="set_hold", status="skipped", rule="hold_off", reason="Someone picked Away by hand",
                        request={"kind": MANUAL_KIND, "source": "homekit", "climate_ref": "away", "heat_f": 62.0,
                                 "cool_f": 82.0, "hold_type": "homekit_manual", "start": ts.isoformat(),
                                 "until": None},
                        completed_at=ts)
    db.add(row)
    db.commit()
    return row


async def test_homekit_hand_change_is_a_persons_hold_while_homekit_is_the_live_path(db):
    setup_house(db, act_units=["up"])
    put_homekit_snapshot(db, "up", NOW - timedelta(minutes=1))
    row = homekit_hand_change(db, NOW - timedelta(minutes=3))
    ph = load_house_state(db, NOW).units["up"].person_hold
    assert (ph.by, ph.until, ph.since, ph.detection_id, ph.climate_ref) == (
        "thermostat", None, NOW - timedelta(minutes=3), row.id, "away")
    # a write after it (ours or the owner's) supersedes it
    our_hold(db, "up", NOW - timedelta(minutes=2), 68.0, 77.0, channel="homekit")
    db.commit()
    assert load_house_state(db, NOW).units["up"].person_hold is None

    # the cloud is back and shows no hold: that person's hold just ends, no back-off
    db.execute(update(ControlAction).where(ControlAction.actor == "controller").values(status="failed"))
    db.commit()
    put_snapshot(db, "up", NOW + timedelta(minutes=5))
    db.commit()
    unit = load_house_state(db, NOW + timedelta(minutes=6)).units["up"]
    assert unit.person_hold is None and unit.resume_backoff_until is None


async def test_a_persons_hold_seen_on_the_cloud_is_kept_while_only_homekit_reports(db):
    setup_house(db, act_units=["up"])
    hold = person(start=NOW - timedelta(minutes=30), end=NOW + timedelta(hours=3), hold_type="holdHours")
    at(db, NOW, heat=70.0, cool=74.0, hold=hold)
    await controller.tick(FakeSource(), NOW)
    (det,) = skipped(db, MANUAL_KIND)
    put_homekit_snapshot(db, "up", NOW + timedelta(minutes=10))  # HomeKit shows no ecobee holds
    db.commit()
    ph = load_house_state(db, NOW + timedelta(minutes=11)).units["up"].person_hold
    assert ph.detection_id == det.id and ph.until == NOW + timedelta(hours=3)
    assert load_house_state(db, NOW + timedelta(hours=3, minutes=1)).units["up"].person_hold is None


async def test_utility_event_by_its_clock_window_stops_writes_when_the_snapshot_lags(db):
    setup_house(db, act_units=["up", "main"])
    utility_event(db, "up", NOW - timedelta(minutes=2), NOW + timedelta(hours=2), status="announced")
    src = FakeSource()
    await controller.tick(src, NOW)
    assert [h.unit_key for h in src.holds] == ["main"]
    assert "utility event 'Peak saver'" in controller.current_plan(db, NOW)[1].guard.blocked_reason


@pytest.mark.parametrize("case", ["person", "backoff", "vacation", "event_clock", "unknown"])
async def test_sensor_sets_wait_while_the_controller_stands_aside(db, case):
    setup_house(db, NIGHT, act_units=["up"])
    snapshots_with_sets(db, NIGHT, DAY_SETS)
    ts = NIGHT - timedelta(minutes=1)
    if case == "person":
        put_snapshot(db, "up", ts, sensor_sets={"home": DAY_SETS["up"]},
                     hold=person(start=NIGHT - timedelta(hours=1), end=INDEFINITE_END, hold_type="indefinite"))
    elif case == "backoff":
        owner_resume(db, "resume_schedule", NIGHT - timedelta(minutes=30))
    elif case == "vacation":
        put_snapshot(db, "up", ts, sensor_sets={"home": DAY_SETS["up"]},
                     hold=HoldInfo(heat_f=60.0, cool_f=84.0, hold_type="vacation"))
    elif case == "event_clock":
        utility_event(db, "up", NIGHT - timedelta(minutes=5), NIGHT + timedelta(hours=1))
    else:
        put_snapshot(db, "up", ts, sensor_sets={"home": DAY_SETS["up"]},
                     hold=HoldInfo(heat_f=66.0, cool_f=80.0, hold_type="switchOccupancy"))
    db.commit()
    src = SetsSource()
    await controller.tick(src, NIGHT)
    assert src.set_writes == [] and src.holds == []


@pytest.mark.parametrize("case", ["ours", "smart_away"])
async def test_sensor_sets_are_written_under_our_hold_or_smart_away(db, case):
    setup_house(db, NIGHT, act_units=["up"])
    snapshots_with_sets(db, NIGHT, DAY_SETS)
    ts = NIGHT - timedelta(minutes=1)
    if case == "ours":
        our_hold(db, "up", NIGHT - timedelta(minutes=40), 67.0, 74.0)
        hold = HoldInfo(heat_f=67.0, cool_f=74.0, start=NIGHT - timedelta(minutes=40),
                        end=NIGHT + timedelta(minutes=80))
        put_snapshot(db, "up", ts, heat=67.0, cool=74.0, sensor_sets={"home": DAY_SETS["up"]}, hold=hold)
    else:
        put_snapshot(db, "up", ts, heat=62.0, cool=82.0, sensor_sets={"home": DAY_SETS["up"]},
                     hold=HoldInfo(kind="climate", climate_ref="away", hold_type="autoAway"))
    db.commit()
    src = SetsSource()
    await controller.tick(src, NIGHT)
    assert src.set_writes == [("up", {"home": ["up.girls_room"]})]

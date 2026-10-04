"""Pre-cool / pre-heat before an announced utility event (spec §2): the policy's event_prep
target (window, direction, skip, the one-hour fit) and the controller's hold sizing (it ends by
hold_end_by; renewals and the HomeKit fallback obey the same fit)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from climate.control import controller
from climate.control.policy import PolicyParams, UtilityEventState, plan
from climate.sources.base import HoldInfo
from climate.state import load_house_state
from climate.store.app_settings import UtilityEventSettings, put_setting
from climate.store.orm import ControlAction, UtilityEvent
from tests.test_controller_safety import circuit_open
from tests.test_controller_tick import NOW, FakeSource, actions, put_snapshot, refresh_sensors, setup_house
from tests.test_policy_plan import at, by_unit, make_state, snap

PREP = UtilityEventSettings(precondition=True)  # 2°F, a 2 h hold, ending 10 min before the start


def event(start: datetime, unit: str = "up", **kw) -> UtilityEventState:
    base = {"id": 7, "unit_key": unit, "event_key": "link:x", "name": "Peak saver", "status": "announced",
            "start_at": start, "end_at": start + timedelta(hours=3), "is_relative": True, "cool_offset_f": 2.0}
    return UtilityEventState(**{**base, **kw})


def prep_state(now: datetime, events: list[UtilityEventState], *, utility: UtilityEventSettings = PREP,
               hvac: str = "cool", up_hold: HoldInfo | None = None, **kw):
    snaps = {k: snap(k, now, heat=68.0, cool=77.0, hvac=hvac, hold=up_hold if k == "up" else None)
             for k in ("main", "up", "bed")}
    return make_state(now, occupied=("toy_room",), snapshots=snaps, utility_events=events, utility=utility, **kw)


# ---------------------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------------------


def test_pre_cooling_target_inside_the_window():
    now = at(14)
    t = by_unit(plan(prep_state(now, [event(at(16))])))
    up = t["up"]
    assert up.rule == "event_prep" and up.desired == "hold" and up.hold_end_by == at(15, 50)
    assert (up.heat_f, up.cool_f) == (68.0, 75.0)  # day comfort 68–77, cooling 2°F lower
    assert up.reason == ("Pre-cooling 2°F before the utility event at 4:00 PM; this hold ends by 3:50 PM, before "
                         "the event starts.")
    assert t["main"].rule != "event_prep" and t["bed"].rule != "event_prep"  # the event is upstairs only


@pytest.mark.parametrize(("now", "prepped"), [
    (at(13, 34), False),  # before the window (end_by 3:50 PM - 2 h - 15 min = 1:35 PM)
    (at(13, 35), True),
    (at(14, 50), True),  # exactly one hour still fits
    (at(14, 51), False),  # a one-hour hold no longer fits and none of ours is running
    (at(15, 50), False),  # end_by itself: the event is near, the target is the normal one
])
def test_the_window_and_the_one_hour_fit(now, prepped):
    up = by_unit(plan(prep_state(now, [event(at(16))])))["up"]
    assert (up.rule == "event_prep") is prepped
    if not prepped:
        assert up.cool_f == 77.0 and up.hold_end_by is None


def test_a_running_prep_hold_keeps_the_target_so_it_lapses_on_its_own():
    ours = HoldInfo(heat_f=68.0, cool_f=75.0, start=at(13, 40), end=at(15, 40), hold_type="holdHours", set_by_us=True)
    up = by_unit(plan(prep_state(at(15, 25), [event(at(16))], up_hold=ours)))["up"]
    assert up.rule == "event_prep" and up.hold_end_by == at(15, 50)
    # a hold of ours from before the window is not a prep hold
    older = ours.model_copy(update={"start": at(12, 0), "end": at(15, 40)})
    assert by_unit(plan(prep_state(at(15, 25), [event(at(16))], up_hold=older)))["up"].rule != "event_prep"


@pytest.mark.parametrize(("ev", "hvac", "outdoor", "direction"), [
    ({"cool_offset_f": 2.0}, "cool", None, "cool"),
    ({"cool_offset_f": 2.0}, "heat", None, None),  # a cooling event on a heating unit: no pre-cooling
    ({"cool_offset_f": 2.0}, "auto", 50.0, None),  # auto, heating weather: likewise
    ({"cool_offset_f": None, "heat_offset_f": -2.0}, "cool", None, None),
    ({"cool_offset_f": None, "heat_offset_f": -2.0}, "heat", None, "heat"),
    ({"cool_offset_f": None, "is_cool_off": True}, "auto", None, "cool"),
    ({"is_relative": False, "cool_offset_f": None, "heat_f": 64.0}, "auto", None, "heat"),
    ({"is_relative": False, "cool_offset_f": None, "heat_f": 64.0, "cool_f": 80.0}, "cool", None, "cool"),
    ({"is_relative": False, "cool_offset_f": None, "heat_f": 64.0, "cool_f": 80.0}, "heat", None, "heat"),
    ({"cool_offset_f": None}, "auxHeatOnly", None, "heat"),
    ({"cool_offset_f": None}, "auto", 72.0, "cool"),
    ({"cool_offset_f": None}, "auto", 50.0, "heat"),
    ({"cool_offset_f": None}, "auto", None, None),  # nothing tells: no prep
])
def test_direction(ev, hvac, outdoor, direction):
    state = prep_state(at(14), [event(at(16), **ev)], hvac=hvac, outdoor_temp_f=outdoor)
    up = by_unit(plan(state))["up"]
    if direction is None:
        assert up.rule != "event_prep"
    elif direction == "cool":
        assert up.rule == "event_prep" and (up.heat_f, up.cool_f) == (68.0, 75.0)
        assert up.reason.startswith("Pre-cooling 2°F")
    else:
        assert up.rule == "event_prep" and (up.heat_f, up.cool_f) == (70.0, 77.0)
        assert up.reason.startswith("Pre-heating 2°F")


@pytest.mark.parametrize(("kw", "prepped"), [
    ({"skip": "requested"}, False),
    ({"skip": "done"}, False),
    ({"skip": "refused"}, True),  # mandatory: it will run, so preparing still helps
    ({"status": "running"}, False),
    ({"start_at": None}, False),
])
def test_skips_and_states(kw, prepped):
    up = by_unit(plan(prep_state(at(14), [event(at(16), **kw)])))["up"]
    assert (up.rule == "event_prep") is prepped


def test_off_by_default_and_never_on_a_thermostat_that_is_off():
    assert by_unit(plan(prep_state(at(14), [event(at(16))], utility=UtilityEventSettings())))["up"].rule != "event_prep"
    state = prep_state(at(14), [event(at(16))], hvac="off")
    assert by_unit(plan(state))["up"].rule == "hold_off"


def test_tunables_come_from_the_utility_settings():
    cfg = UtilityEventSettings(precondition=True, precondition_degrees_f=1.5, precondition_hours=1,
                               precondition_end_gap_min=30)
    up = by_unit(plan(prep_state(at(14, 20), [event(at(16))], utility=cfg)))["up"]
    assert up.hold_end_by == at(15, 30) and up.cool_f == 75.5
    assert "this hold ends by 3:30 PM" in up.reason
    # window: 3:30 PM - 1 h - 15 min = 2:15 PM
    assert by_unit(plan(prep_state(at(14, 14), [event(at(16))], utility=cfg)))["up"].rule != "event_prep"


# ---------------------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------------------


def prep_house(db, start: datetime) -> None:
    setup_house(db, act_units=["up"])
    put_setting(db, "utility_events", PREP)
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=68.0, cool=77.0)
    db.add(UtilityEvent(unit_key="up", event_key="link:x", name="Peak saver", status="announced", start_at=start,
                        end_at=start + timedelta(hours=3), first_seen_at=NOW - timedelta(hours=5),
                        last_seen_at=NOW, is_relative=True, cool_offset_f=2.0, is_optional=True, detail={}))
    db.commit()


@pytest.mark.parametrize(("start_in", "hours"), [(timedelta(hours=2, minutes=20), 2), (timedelta(hours=1, minutes=30), 1)])
async def test_prep_hold_is_sized_to_end_before_the_event(db, start_in, hours):
    prep_house(db, NOW + start_in)
    src = FakeSource()
    await controller.tick(src, NOW)
    (hold,) = src.holds
    assert (hold.unit_key, hold.heat_f, hold.cool_f, hold.hours) == ("up", 68.0, 75.0, hours)
    end_by = NOW + start_in - timedelta(minutes=10)
    assert NOW + timedelta(hours=hold.hours) <= end_by
    row = actions(db, unit_key="up", status="verified")[0]
    assert row.rule == "event_prep" and row.request["hours"] == hours
    assert row.request["hold_end_by"] == end_by.isoformat()


async def test_no_renewal_runs_past_the_event_start(db):
    start = NOW + timedelta(hours=2, minutes=20)  # end_by NOW + 2 h 10 min
    prep_house(db, start)
    src = FakeSource()
    await controller.tick(src, NOW)
    assert [h.hours for h in src.holds] == [2]  # ends NOW + 2 h

    t = NOW + timedelta(minutes=105)  # our hold ends in 15 min: renewal time, but no hour fits any more
    ours = HoldInfo(heat_f=68.0, cool_f=75.0, start=NOW, end=NOW + timedelta(hours=2), hold_type="holdHours")
    put_snapshot(db, "up", t - timedelta(minutes=1), heat=68.0, cool=75.0, hold=ours)
    for u in ("main", "bed"):
        put_snapshot(db, u, t - timedelta(minutes=1))
    refresh_sensors(db, t)
    await controller.tick(src, t)
    assert len(src.holds) == 1
    state = load_house_state(db, t)
    target = next(x for x in plan(state) if x.unit_key == "up")
    assert target.rule == "event_prep"
    ev = controller._evaluate(state, state.units["up"], target, t, "act", NOW + timedelta(hours=2))
    assert ev.kind == "none"
    assert ev.note == "Too close to the utility event for another pre-cooling hold; it ends on its own before the event."


def test_fit_hours():
    end_by = NOW + timedelta(hours=2)
    assert controller.fit_hours(NOW, end_by, 2) == 2
    assert controller.fit_hours(NOW + timedelta(minutes=1), end_by, 2) == 1
    assert controller.fit_hours(NOW + timedelta(minutes=61), end_by, 2) is None
    assert controller.fit_hours(NOW, end_by, 1) == 1


async def test_homekit_fallback_queues_a_prep_hold_only_until_the_end_by(db):
    prep_house(db, NOW + timedelta(hours=1, minutes=30))  # end_by NOW + 80 min: one hour fits
    circuit_open(db)
    await controller.tick(FakeSource(kind="ecobee"), NOW)
    (row,) = actions(db, channel="homekit")
    until = datetime.fromisoformat(row.request["until"])
    assert until == NOW + timedelta(hours=1) and until <= NOW + timedelta(minutes=80)
    assert row.rule == "event_prep"


# ---------------------------------------------------------------------------------------
# two events on one unit, an event moved earlier, our hold lapsing as the window closes
# ---------------------------------------------------------------------------------------


def test_prep_is_only_for_the_next_event_never_a_later_one():
    """Event A at 2:08 PM (its window closed at 1:58 PM), event B at 3:48 PM: at 2:00 PM the
    target is the normal one, not a pre-cool for B that would still run when A starts."""
    a = event(at(14, 8), event_key="link:a", end_at=at(15, 8))
    b = event(at(15, 48), id=8, event_key="link:b")
    up = by_unit(plan(prep_state(at(14), [b, a])))["up"]
    assert up.rule != "event_prep" and up.cool_f == 77.0 and up.hold_end_by is None
    # inside A's own window it is A's prep, ending before A
    up = by_unit(plan(prep_state(at(12, 30), [b, a])))["up"]
    assert up.rule == "event_prep" and up.hold_end_by == at(13, 58)


@pytest.mark.parametrize(("skip", "prepped"), [("requested", False), ("done", True)])
def test_a_pending_skip_on_the_next_event_still_counts(skip, prepped):
    """A skip still pending on the next event (the opt-out may fail) means no prep for a later
    one either; an event already opted out of is no longer the next."""
    a = event(at(14, 30), event_key="link:a", end_at=at(15), skip=skip)
    b = event(at(15, 50), id=8, event_key="link:b")
    up = by_unit(plan(prep_state(at(14), [a, b])))["up"]
    assert (up.rule == "event_prep") is prepped
    if prepped:
        assert up.hold_end_by == at(15, 40)


async def test_our_prep_hold_is_pulled_back_when_an_earlier_event_appears(db):
    """Our 2 h pre-cool hold for a 4:20 PM event runs to 4:00 PM; at 2:12 PM ecobee lists the
    event for 2:25 PM instead. Within the 30-min limit, the controller resumes the schedule
    (never a new hold) before the event starts."""
    setup_house(db, act_units=["up"])
    put_setting(db, "utility_events", PREP)
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=68.0, cool=77.0)
    db.add(UtilityEvent(unit_key="up", event_key="link:old", name="Peak saver", status="announced",
                        start_at=NOW + timedelta(hours=2, minutes=20), end_at=NOW + timedelta(hours=4),
                        first_seen_at=NOW - timedelta(hours=5), last_seen_at=NOW, is_relative=True, cool_offset_f=2.0,
                        is_optional=True, detail={}))
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    (prep,) = src.holds
    assert (prep.cool_f, prep.hours) == (75.0, 2)

    db.execute(UtilityEvent.__table__.update().values(status="cancelled", ended_at=NOW))
    db.add(UtilityEvent(unit_key="up", event_key="link:new", name="Peak saver", status="announced",
                        start_at=NOW + timedelta(minutes=25), end_at=NOW + timedelta(hours=2, minutes=25),
                        first_seen_at=NOW + timedelta(minutes=11), last_seen_at=NOW + timedelta(minutes=11),
                        is_relative=True, cool_offset_f=2.0, is_optional=True, detail={}))
    db.commit()
    t = NOW + timedelta(minutes=12)
    ours = HoldInfo(heat_f=68.0, cool_f=75.0, start=NOW, end=NOW + timedelta(hours=2), hold_type="holdHours")
    put_snapshot(db, "up", t - timedelta(minutes=1), heat=68.0, cool=75.0, hold=ours)
    for u in ("main", "bed"):
        put_snapshot(db, u, t - timedelta(minutes=1))
    refresh_sensors(db, t)
    await controller.tick(src, t)
    assert src.resumes == ["up"] and src.forced == [False] and len(src.holds) == 1
    row = actions(db, unit_key="up", action="resume_program")[0]
    assert row.status == "verified"
    assert row.reason.endswith("Our pre-conditioning hold runs until 4:00 PM, past the start of the utility event at "
                               "2:25 PM; it is released now, so the event applies to the normal setpoint.")


def test_a_running_prep_hold_is_released_when_an_earlier_event_is_announced():
    """Our pre-cool hold for a 4:00 PM event runs 1:50-3:50 PM; at 2:00 PM an event is
    announced for 2:10 PM: the controller releases it (it was sized for the later event)."""
    from climate.store.app_settings import ControlSettings

    ours = HoldInfo(heat_f=68.0, cool_f=75.0, start=at(13, 50), end=at(15, 50), hold_type="holdHours", set_by_us=True)
    later = event(at(16), event_key="link:later")
    sooner = event(at(14, 10), id=8, event_key="link:sooner", end_at=at(15, 10))
    state = prep_state(at(14), [sooner, later], up_hold=ours, control=ControlSettings(mode="act"))
    state.units["up"].last_change_at = at(13, 50)  # 10 min ago: inside the rate limit
    up = by_unit(plan(state))["up"]
    assert up.rule != "event_prep"
    ev = controller._evaluate(state, state.units["up"], up, at(14), "act", at(15, 50), ours_rule="event_prep")
    assert ev.kind == "resume" and ev.urgent and "past the start of the utility event at 2:10 PM" in ev.note
    # a hold of ours that is not a pre-conditioning one is left to the normal rules (and the limit)
    ev = controller._evaluate(state, state.units["up"], up, at(14), "act", at(15, 50), ours_rule="comfort")
    assert ev.kind == "none" and not ev.urgent and "at most one change per 30 min" in ev.guard.blocked_reason


async def test_our_hold_past_its_end_waits_for_the_next_snapshot(db):
    """Pre-cooling 3°F with the default 2°F step: the snapshot taken just before our prep hold
    lapsed still shows it. The controller writes nothing (no step back from the stale 74°F,
    which would be a 2 h hold still below normal running into the event)."""
    setup_house(db, act_units=["up"])
    put_setting(db, "utility_events", UtilityEventSettings(precondition=True, precondition_degrees_f=3.0))
    start = NOW + timedelta(hours=2)  # 4:00 PM; end_by 3:50 PM
    db.add(UtilityEvent(unit_key="up", event_key="link:x", name="Peak saver", status="announced", start_at=start,
                        end_at=start + timedelta(hours=3), first_seen_at=NOW - timedelta(hours=5), last_seen_at=NOW,
                        is_relative=True, cool_offset_f=2.0, is_optional=True, detail={}))
    db.commit()
    t = NOW + timedelta(hours=1, minutes=51)
    prep = HoldInfo(heat_f=68.0, cool_f=74.0, start=NOW - timedelta(minutes=10), end=t - timedelta(minutes=1),
                    hold_type="holdHours", set_by_us=True)
    put_snapshot(db, "up", t - timedelta(minutes=2), heat=68.0, cool=74.0, hold=prep)
    for u in ("main", "bed"):
        put_snapshot(db, u, t - timedelta(minutes=2))
    refresh_sensors(db, t)
    src = FakeSource()
    await controller.tick(src, t)
    assert [h for h in src.holds if h.unit_key == "up"] == [] and src.resumes == []
    plan_row = {r.target.unit_key: r for r in controller.current_plan(db, t)}["up"]
    assert not plan_row.would_write


def test_a_hot_day_pre_cool_hold_also_ends_before_the_next_event():
    """Rule 4 (hot-day pre-cool) lowers the cooling setpoint too: with an event announced its
    hold must end before the event, whether or not pre-conditioning before events is on."""
    hot = {"forecast_high_f": 96.0, "forecast_sunny": True,
           "policy": PolicyParams(precool_enabled=True, precool_degrees_f=1.0, precool_start_hour=13)}
    off = UtilityEventSettings(precondition=False)
    up = by_unit(plan(prep_state(at(14), [event(at(15))], utility=off, **hot)))["up"]
    assert up.rule == "precool" and up.cool_f == 76.0 and up.hold_end_by == at(14, 50)
    assert up.reason.endswith("This hold ends by 2:50 PM, before the utility event at 3:00 PM.")
    # no event announced: the rule is unchanged
    alone = by_unit(plan(prep_state(at(14), [], utility=off, **hot)))["up"]
    assert alone.rule == "precool" and alone.hold_end_by is None


async def test_a_hot_day_pre_cool_hold_of_ours_is_pulled_back_before_an_event(db):
    """Our hot-day pre-cool hold (rule 'precool') would still run when a newly announced event
    starts: the controller resumes the schedule now, inside the rate limit, never a new hold."""
    setup_house(db, act_units=["up"])
    put_setting(db, "utility_events", UtilityEventSettings(precondition=False))
    ours = HoldInfo(heat_f=68.0, cool_f=76.0, start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=110),
                    hold_type="holdHours")
    db.add(ControlAction(ts=NOW - timedelta(minutes=10), unit_key="up", actor="controller", mode="act",
                         channel="simulator", action="set_hold", status="verified", rule="precool",
                         reason="Pre-cooling on a hot afternoon.", completed_at=NOW - timedelta(minutes=10),
                         request={"unit_key": "up", "heat_f": 68.0, "cool_f": 76.0, "hours": 2}))
    db.add(UtilityEvent(unit_key="up", event_key="link:soon", name="Peak saver", status="announced",
                        start_at=NOW + timedelta(minutes=40), end_at=NOW + timedelta(hours=3),
                        first_seen_at=NOW, last_seen_at=NOW, is_relative=True, cool_offset_f=2.0,
                        is_optional=True, detail={}))
    db.commit()
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=68.0, cool=76.0, hold=ours)
    for u in ("main", "bed"):
        put_snapshot(db, u, NOW - timedelta(minutes=1))
    refresh_sensors(db, NOW)
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.resumes == ["up"] and src.forced == [False] and src.holds == []
    row = actions(db, unit_key="up", action="resume_program")[0]
    assert "Our pre-cooling hold runs until" in row.reason and "past the start of the utility event" in row.reason

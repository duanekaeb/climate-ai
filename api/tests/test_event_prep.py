"""Pre-cool / pre-heat before an announced utility event (spec §2): the policy's event_prep
target (window, direction, skip, the one-hour fit) and the controller's hold sizing (it ends by
hold_end_by; renewals and the HomeKit fallback obey the same fit)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from climate.control import controller
from climate.control.policy import UtilityEventState, plan
from climate.sources.base import HoldInfo
from climate.state import load_house_state
from climate.store.app_settings import UtilityEventSettings, put_setting
from climate.store.orm import UtilityEvent
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
    ({"cool_offset_f": 2.0}, "heat", None, "cool"),  # the event's own direction wins over the mode
    ({"cool_offset_f": None, "heat_offset_f": -2.0}, "cool", None, "heat"),
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

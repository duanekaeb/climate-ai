"""policy.plan scenarios (blueprint §2-§3). Pure: HouseState in, one UnitTarget per unit out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from climate.control.policy import HouseState, PolicyParams, RoomStatus, UnitStatus, UnitTarget, plan
from climate.house import ROOMS
from climate.sources.base import HoldInfo, UnitSnapshot
from climate.store.app_settings import ControlSettings, OccupancySettings

TZ = "America/Chicago"


def at(hh: int, mm: int = 0, day: int = 15) -> datetime:
    """July 2026 local -> UTC. July 15 2026 is a Wednesday (a school and office day)."""
    return datetime(2026, 7, day, hh, mm, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def snap(unit_key: str, now: datetime, heat: float = 60.0, cool: float = 84.0, hold: HoldInfo | None = None,
         hvac: str = "cool", **kw) -> UnitSnapshot:
    return UnitSnapshot(unit_key=unit_key, ts=now, source="simulator", hvac_mode=hvac, heat_sp_f=heat, cool_sp_f=cool,
                        hold=hold, zone_humidity=45.0, **kw)


def make_state(
    now: datetime, *, occupied: tuple[str, ...] = (), asleep: tuple[str, ...] = (), offsets: dict | None = None,
    stale: tuple[str, ...] = (), since: dict | None = None, house_empty: bool = False,
    policy: PolicyParams | None = None, snapshots: dict | None = None, **kw,
) -> HouseState:
    rooms = {}
    for r in ROOMS:
        if not r.has_comfort_target:
            st = "no_target"
        elif r.key in asleep:
            st = "asleep"
        elif r.key in occupied:
            st = "occupied"
        elif not r.has_sensor:
            st = "unknown"
        else:
            st = "empty"
        rooms[r.key] = RoomStatus(
            room_key=r.key, name=r.name, unit_key=r.unit_key, floor=r.floor, has_sensor=r.has_sensor,
            is_sleep_room=r.is_sleep_room, has_comfort_target=r.has_comfort_target, state=st,
            offset_f=(offsets or {}).get(r.key), stale=r.key in stale,
            since=(since or {}).get(r.key, now - timedelta(hours=1)),
        )
    snapshots = snapshots or {}
    units = {k: UnitStatus(unit_key=k, name=k, snapshot=snapshots.get(k)) for k in ("main", "up", "bed")}
    return HouseState(
        now=now, tz=TZ, units=units, rooms=rooms, house_empty=house_empty,
        house_empty_reason="Phones away and no motion anywhere for 52 min." if house_empty else "",
        control=kw.pop("control", ControlSettings()), occupancy=kw.pop("occupancy", OccupancySettings()),
        policy=policy or PolicyParams(), **kw,
    )


def by_unit(targets: list[UnitTarget]) -> dict[str, UnitTarget]:
    return {t.unit_key: t for t in targets}


def test_one_target_per_unit_in_order():
    targets = plan(make_state(at(14)))
    assert [t.unit_key for t in targets] == ["main", "up", "bed"]
    assert all(t.reason.endswith(".") and len(t.reason) > 20 for t in targets)


def test_linked_floors_main_empty_by_day_upstairs_occupied():
    now = at(14)
    state = make_state(now, occupied=("toy_room",), since={"hallway": at(13, 10), "school_room": at(12, 40)},
                       policy=PolicyParams(linked_offset_f=2.0))
    t = by_unit(plan(state))
    assert t["up"].rule == "comfort" and (t["up"].heat_f, t["up"].cool_f) == (68.0, 77.0)
    main = t["main"]
    assert main.rule == "linked_floors"
    assert main.cool_f == 75.0  # upstairs 77 - 2, cooler than its own day band
    assert main.heat_f == 66.0  # upstairs heat 68 - linked_heat_gap 2
    assert main.reason.startswith("Main floor empty since 1:10 PM and the Toy Room is occupied")
    assert "2°F under upstairs (75°F)" in main.reason


def test_linked_floors_never_warmer_than_main_day_band():
    t = by_unit(plan(make_state(at(14), occupied=("girls_room",))))
    assert t["main"].rule == "linked_floors"
    assert t["main"].cool_f == 76.0  # min(main day 76, up 77 - 1)
    assert t["main"].cool_f < 80.0  # never floats to its away band


def test_linked_floors_follows_upstairs_offset():
    # the Toy Room runs 1°F warm: upstairs cools to 76, main follows 1°F under it
    t = by_unit(plan(make_state(at(14), occupied=("toy_room",), offsets={"toy_room": 1.0})))
    assert t["up"].cool_f == 76.0 and "Toy Room runs warm" in t["up"].reason
    assert t["main"].cool_f == 75.0


def test_no_link_when_nobody_upstairs_or_disabled():
    t = by_unit(plan(make_state(at(14))))
    assert t["main"].rule == "comfort" and t["main"].cool_f == 76.0
    t = by_unit(plan(make_state(at(14), occupied=("toy_room",), policy=PolicyParams(linked_floors_enabled=False))))
    assert t["main"].rule == "comfort"


def test_unsensored_rooms_do_not_make_the_main_floor_occupied():
    # Twins'/Olive's rooms are 'unknown' by day: the main floor is still empty -> linked
    t = by_unit(plan(make_state(at(14), occupied=("toy_room",))))
    assert t["main"].rule == "linked_floors"


def test_stale_room_counts_as_occupied():
    t = by_unit(plan(make_state(at(14), occupied=("toy_room",), stale=("kitchen",))))
    assert t["main"].rule == "comfort"
    assert "Kitchen" in t["main"].reason


def test_night_sleep_comfort_on_all_units_with_hallway():
    now = at(22, 30)
    # room offsets elsewhere on the main floor are ignored at night: the Hallway steers
    state = make_state(now, asleep=("girls_room", "bedroom", "twins_room", "olive_room"),
                       offsets={"kitchen": 2.5, "hallway": 0.5})
    t = by_unit(plan(state))
    assert {u.rule for u in t.values()} == {"sleep"}
    assert (t["main"].heat_f, t["main"].cool_f) == (66.5, 73.5)  # night 67/74 minus the Hallway's 0.5
    assert "Hallway" in t["main"].reason and "Twins' Room" in t["main"].reason
    assert t["main"].priority_room == "hallway"
    assert (t["up"].heat_f, t["up"].cool_f) == (67.0, 74.0)
    assert t["up"].priority_room == "girls_room"
    assert (t["bed"].heat_f, t["bed"].cool_f) == (66.0, 73.0)


def test_main_floor_is_not_empty_at_night_even_without_signals():
    # 20:15: the Twins'/Olive's windows (20:00) have started, Girls' (20:30) not yet
    t = by_unit(plan(make_state(at(20, 15), occupied=("toy_room",))))
    assert t["main"].rule == "sleep"
    assert t["up"].rule == "comfort"


def test_house_empty_sets_back_with_gap():
    t = by_unit(plan(make_state(at(14), house_empty=True, policy=PolicyParams(setback_gap_f=3.0))))
    assert {u.rule for u in t.values()} == {"house_setback"}
    assert (t["up"].heat_f, t["up"].cool_f) == (62.0, 82.0)
    assert t["main"].cool_f == 79.0  # up 82 - gap 3
    assert t["main"].cool_f < t["up"].cool_f
    assert (t["bed"].heat_f, t["bed"].cool_f) == (62.0, 80.0)
    assert "House empty" in t["main"].reason and "3°F under upstairs" in t["main"].reason


def test_bed_wing_is_independent():
    # main floor occupied and upstairs empty: the wing still holds its own comfort
    t = by_unit(plan(make_state(at(10), occupied=("office", "living_room"))))
    assert t["bed"].rule == "independent"
    assert (t["bed"].heat_f, t["bed"].cool_f) == (68.0, 76.0)
    assert t["bed"].priority_room == "office" and "office hours" in t["bed"].reason
    t2 = by_unit(plan(make_state(at(10), occupied=("office",), policy=PolicyParams(bed_wing_independent=False))))
    assert t2["bed"].rule == "comfort"


def test_priority_room_by_schedule():
    t = by_unit(plan(make_state(at(10), occupied=("school_room",), offsets={"school_room": 1.5})))
    assert t["main"].priority_room == "school_room"
    assert t["main"].cool_f == 74.5  # 76 - 1.5 keeps the warm School Room in band
    t = by_unit(plan(make_state(at(18), occupied=("living_room",))))
    assert t["main"].priority_room == "living_room" and "evening" in t["main"].reason


def test_conflicting_offsets_resolved_by_priority():
    # rooms 6°F apart on one thermostat can't all fit a 76/68 band with a 3°F deadband
    state = make_state(at(10), occupied=("school_room", "kitchen"), offsets={"school_room": 3.0, "kitchen": -3.0})
    main = by_unit(plan(state))["main"]
    assert main.cool_f == 73.0 and main.heat_f == 65.0  # steered for the School Room (school hours)
    assert "disagree" in main.reason


def test_precool_on_hot_sunny_afternoons_only():
    p = PolicyParams(precool_enabled=True, precool_degrees_f=1.0, precool_start_hour=13)
    hot = dict(forecast_high_f=96.0, forecast_sunny=True)
    t = by_unit(plan(make_state(at(14), occupied=("toy_room",), policy=p, **hot)))
    assert t["up"].rule == "precool" and t["up"].cool_f == 76.0
    assert t["main"].cool_f == 75.0  # linked to the pre-cooled upstairs
    # before the start hour, on a mild day, or when disabled: no pre-cool
    assert by_unit(plan(make_state(at(12), occupied=("toy_room",), policy=p, **hot)))["up"].rule == "comfort"
    mild = dict(forecast_high_f=85.0, forecast_sunny=True)
    assert by_unit(plan(make_state(at(14), occupied=("toy_room",), policy=p, **mild)))["up"].rule == "comfort"
    assert by_unit(plan(make_state(at(14), occupied=("toy_room",), **hot)))["up"].rule == "comfort"


def test_recovery_main_floor_leads():
    now = at(14)
    occ = OccupancySettings(phones_away=False, phones_updated_at=now - timedelta(minutes=5))
    up_setback = snap("up", now, heat=62.0, cool=82.0, hold=HoldInfo(heat_f=62.0, cool_f=82.0, set_by_us=True))
    state = make_state(now, occupied=("kitchen",), since={"kitchen": now - timedelta(minutes=3)}, occupancy=occ,
                       snapshots={"up": up_setback}, policy=PolicyParams(recovery_lead_min=20))
    t = by_unit(plan(state))
    assert t["up"].rule == "recovery" and (t["up"].heat_f, t["up"].cool_f) == (62.0, 82.0)
    assert "until 2:15 PM" in t["up"].reason  # phones home at 1:55 + 20 min
    assert t["main"].rule == "recovery" and t["main"].cool_f == 76.0
    # after the lead, upstairs recovers too
    later = make_state(now + timedelta(minutes=25), occupied=("kitchen",), occupancy=occ,
                       snapshots={"up": up_setback.model_copy(update={"ts": now + timedelta(minutes=25)})})
    assert by_unit(plan(later))["up"].rule == "comfort"
    # someone upstairs: no waiting
    busy = make_state(now, occupied=("kitchen", "toy_room"), occupancy=occ, snapshots={"up": up_setback})
    assert by_unit(plan(busy))["up"].rule == "comfort"


def test_desired_program_when_schedule_already_matches():
    now = at(14)
    snaps = {"main": snap("main", now, heat=68.0, cool=76.0),  # no hold, matches the plan
             "up": snap("up", now, heat=68.0, cool=78.0)}  # no hold, differs
    t = by_unit(plan(make_state(now, snapshots=snaps)))
    assert t["main"].desired == "program"
    assert t["up"].desired == "hold"
    # a hold of ours is active but the source reports the program's setpoints: resume is possible
    held = snap("up", now, heat=66.0, cool=75.0, hold=HoldInfo(heat_f=66.0, cool_f=75.0, set_by_us=True),
                settings={"program_heat_f": 68.0, "program_cool_f": 77.0})
    assert by_unit(plan(make_state(now, snapshots={"up": held})))["up"].desired == "program"


def test_thermostat_off_is_left_alone():
    now = at(14)
    t = by_unit(plan(make_state(now, snapshots={"bed": snap("bed", now, heat=65, cool=79, hvac="off")})))
    assert t["bed"].rule == "hold_off" and t["bed"].desired == "program"
    assert (t["bed"].heat_f, t["bed"].cool_f) == (65.0, 79.0)


def test_targets_are_rounded_to_half_degrees():
    t = by_unit(plan(make_state(at(10), occupied=("school_room",), offsets={"school_room": 1.37})))
    assert t["main"].cool_f * 2 == int(t["main"].cool_f * 2)

"""climate.api.labels: the Live card's "whose hold is this" line for every kind of owner, in
house time (pure function; constructed UnitStatus objects, no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from climate.api.labels import clock, day_time, hold_label, when
from climate.control.policy import PersonHold, UnitStatus, UtilityEventState
from climate.sources.base import HoldInfo, ThermostatEvent, UnitSnapshot

TZ = "America/Chicago"  # CDT = UTC-5 in October
NOW = datetime(2026, 10, 3, 19, 30, tzinfo=UTC)  # Sat 2:30 PM
AT_2_10 = datetime(2026, 10, 3, 19, 10, tzinfo=UTC)
AT_4 = datetime(2026, 10, 3, 21, 0, tzinfo=UTC)
AT_6 = datetime(2026, 10, 3, 23, 0, tzinfo=UTC)
NEVER = datetime(2035, 1, 1, tzinfo=UTC)  # ecobee's "until I change it"


def snap(hold: HoldInfo | None = None, events: list[ThermostatEvent] | None = None, source: str = "ecobee") -> UnitSnapshot:
    return UnitSnapshot(unit_key="up", ts=NOW, source=source, hvac_mode="cool", heat_sp_f=68, cool_sp_f=77,
                        hold=hold, events=events or [])


def unit(snapshot: UnitSnapshot | None = None, person: PersonHold | None = None) -> UnitStatus:
    return UnitStatus(unit_key="up", name="Upstairs", snapshot=snapshot, person_hold=person)


def person(by: str = "thermostat", until: datetime | None = AT_4, since: datetime = AT_2_10,
           hold_type: str = "holdHours") -> PersonHold:
    return PersonHold(since=since, first_seen=since, by=by, hold_type=hold_type, until=until, heat_f=68, cool_f=74,
                      detection_id=7)


def plain(hold_type: str = "holdHours", end: datetime | None = AT_4, set_by_us: bool = False) -> HoldInfo:
    return HoldInfo(heat_f=68, cool_f=74, start=AT_2_10, end=end, hold_type=hold_type, set_by_us=set_by_us)


def label(u: UnitStatus, events: list[UtilityEventState] | None = None) -> tuple[str | None, str | None]:
    return hold_label(u, events or [], TZ, NOW)


def event_row(unit_key: str = "up", **kw) -> UtilityEventState:
    base = {"id": 1, "unit_key": unit_key, "event_key": "link:abc", "name": "Peak saver", "status": "announced",
            "start_at": NOW - timedelta(minutes=5), "end_at": AT_6, "cool_f": 78.0}
    return UtilityEventState(**{**base, **kw})


# --- time wording ----------------------------------------------------------------------------


def test_time_wording_is_house_time():
    assert clock(AT_2_10, TZ) == "2:10 PM"
    assert clock(datetime(2026, 10, 3, 5, 5, tzinfo=UTC), TZ) == "12:05 AM"
    assert when(AT_4, TZ, NOW) == "4:00 PM"
    assert when(datetime(2026, 10, 4, 14, 0, tzinfo=UTC), TZ, NOW) == "Sun 9:00 AM"
    assert day_time(datetime(2026, 10, 12, 14, 0, tzinfo=UTC), TZ) == "Oct 12, 9:00 AM"


# --- nothing running -------------------------------------------------------------------------


def test_no_snapshot_or_no_hold_has_no_label():
    assert label(unit()) == (None, None)
    assert label(unit(snap())) == (None, None)


# --- a person's hold -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ph", "expected"),
    [
        (person(), ("person", "On your hold since 2:10 PM, until 4:00 PM")),
        (person(until=None, hold_type="indefinite"), ("person", "On your hold since 2:10 PM (until you change it)")),
        (person(by="app"), ("app", "Your hold from the app until 4:00 PM")),
        (person(by="app", until=None), ("app", "Your hold from the app (until you change it)")),
        (person(hold_type="quickSave", until=None), ("person", "Quick Save")),
        (person(since=AT_2_10 - timedelta(days=1), until=datetime(2026, 10, 4, 14, 0, tzinfo=UTC)),
         ("person", "On your hold since Fri 2:10 PM, until Sun 9:00 AM")),
    ],
)
def test_person_holds(ph, expected):
    assert label(unit(snap(plain()), ph)) == expected


def test_person_hold_carried_over_a_homekit_snapshot():
    # HomeKit shows no ecobee holds: the detected person's hold still labels the card
    assert label(unit(snap(None, source="homekit"), person(until=None))) == (
        "person", "On your hold since 2:10 PM (until you change it)")


def test_undetected_plain_hold_not_ours_reads_as_a_persons():
    # e.g. the state service is down: no person_hold, and the source does not call it ours
    assert label(unit(snap(plain()))) == ("person", "On your hold since 2:10 PM, until 4:00 PM")
    assert label(unit(snap(plain("indefinite", NEVER)))) == ("person", "On your hold since 2:10 PM (until you change it)")
    assert label(unit(snap(plain("quickSave", None)))) == ("person", "Quick Save")


# --- ours --------------------------------------------------------------------------------------


def test_our_hold():
    assert label(unit(snap(plain(end=datetime(2026, 10, 3, 20, 40, tzinfo=UTC), set_by_us=True)))) == (
        "controller", "Our hold until 3:40 PM")
    assert label(unit(snap(plain(end=None, set_by_us=True)))) == ("controller", "Our hold")
    assert label(unit(snap(plain(end=NEVER, set_by_us=True)))) == ("controller", "Our hold")


# --- ecobee events -------------------------------------------------------------------------------


def dr_hold(**kw) -> HoldInfo:
    base = {"hold_type": "demandResponse", "start": NOW - timedelta(minutes=30), "end": AT_6, "event_name": "Peak saver",
            "is_relative": True, "cool_offset_f": 2.0, "link_ref": "abc"}
    return HoldInfo(**{**base, **kw})


def test_utility_event_from_the_snapshot():
    assert label(unit(snap(dr_hold()))) == ("utility", "Utility event until 6:00 PM (cooling +2°F)")
    # the event list says more (AC off) than the hold does
    ev = ThermostatEvent(event_type="demandResponse", name="Peak saver", running=True, end=AT_6, is_cool_off=True,
                         link_ref="abc")
    assert label(unit(snap(dr_hold(), [ev]))) == ("utility", "Utility event until 6:00 PM (AC off)")
    # nothing reported about the change (a 100% duty cycle changes nothing), and no end: just the event
    bare = dr_hold(is_relative=False, cool_offset_f=None, end=None)
    nothing = ThermostatEvent(event_type="demandResponse", running=True, duty_cycle_pct=100, link_ref="abc")
    assert label(unit(snap(bare, [nothing]))) == ("utility", "Utility event")
    capped = nothing.model_copy(update={"duty_cycle_pct": 50})
    assert label(unit(snap(bare, [capped]))) == ("utility", "Utility event (runtime capped at 50%)")
    # ... but the stored row knows the change and the end
    assert label(unit(snap(bare)), [event_row(status="running")]) == (
        "utility", "Utility event until 6:00 PM (cooling set to 78°F)")


def test_utility_event_on_top_of_a_persons_hold_wins():
    assert label(unit(snap(dr_hold()), person()))[0] == "utility"


def test_utility_event_by_the_clock_when_the_snapshot_lags():
    u = unit(snap(None))
    assert label(u, [event_row()]) == ("utility", "Utility event until 6:00 PM (cooling set to 78°F)")
    relative = event_row(is_relative=True, cool_f=None, heat_offset_f=-2.0)
    assert label(u, [relative]) == ("utility", "Utility event until 6:00 PM (heating −2°F)")
    assert label(u, [event_row(unit_key="main")]) == (None, None)  # another unit's event
    assert label(u, [event_row(skip="done")]) == (None, None)  # opted out
    assert label(u, [event_row(start_at=NOW + timedelta(hours=1))]) == (None, None)  # not started


def test_vacation_smart_away_and_unknown_events():
    vac = HoldInfo(hold_type="vacation", end=datetime(2026, 10, 12, 14, 0, tzinfo=UTC), heat_f=60, cool_f=85)
    assert label(unit(snap(vac))) == ("vacation", "Vacation until Oct 12, 9:00 AM")
    assert label(unit(snap(HoldInfo(hold_type="vacation")))) == ("vacation", "Vacation")
    assert label(unit(snap(HoldInfo(kind="climate", climate_ref="away", hold_type="autoAway")))) == (
        "ecobee_auto", "Smart Away")
    assert label(unit(snap(HoldInfo(kind="climate", climate_ref="home", hold_type="autoHome")))) == (
        "ecobee_auto", "Smart Home")
    assert label(unit(snap(HoldInfo(hold_type="today")), person())) == (
        "unknown_event", "Unrecognised ecobee event: today")

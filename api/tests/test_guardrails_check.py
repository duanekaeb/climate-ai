"""guardrails.check and validate_policy_params: hard limits on every write."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from climate.control.guardrails import check, round_setpoint, validate_policy_params, within_signoff_ranges
from climate.control.policy import PersonHold, UnitStatus, UnitTarget, UtilityEventState
from climate.sources.base import HoldInfo, UnitSnapshot
from climate.store.app_settings import HardLimits

NOW = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)  # 2:00 PM in the house
TZ = "America/Chicago"
LIMITS = HardLimits()


def unit(heat: float | None = 68.0, cool: float | None = 76.0, *, age_min: float = 1, humidity: float = 45.0,
         connected: bool = True, hvac: str = "cool", settings: dict | None = None, hold: HoldInfo | None = None,
         **kw) -> UnitStatus:
    snap = UnitSnapshot(unit_key="up", ts=NOW - timedelta(minutes=age_min), source="simulator", hvac_mode=hvac,
                        heat_sp_f=heat, cool_sp_f=cool, zone_humidity=humidity, connected=connected,
                        settings=settings or {}, hold=hold)
    return UnitStatus(unit_key="up", name="Upstairs", snapshot=snap, age_s=age_min * 60, **kw)


def target(heat: float, cool: float) -> UnitTarget:
    return UnitTarget(unit_key="up", heat_f=heat, cool_f=cool, rule="comfort", reason="test")


def test_round_setpoint():
    assert round_setpoint(75.24) == 75.0
    assert round_setpoint(75.26) == 75.5
    assert round_setpoint(75.75) == 76.0


def test_within_limits_passes_untouched():
    g = check(target(68, 77), unit(68, 76), LIMITS, NOW)
    assert g.ok and not g.clamped and g.violations == [] and g.blocked_reason is None
    assert (g.heat_f, g.cool_f) == (68.0, 77.0)


def test_hard_min_max_clamp():
    g = check(target(58, 86), unit(60, 84), LIMITS, NOW)
    assert (g.heat_f, g.cool_f) == (60.0, 84.0)
    assert g.clamped and g.ok
    assert any("60°F minimum" in v for v in g.violations)
    assert any("84°F maximum" in v for v in g.violations)


def test_step_limit_from_current_setpoint():
    g = check(target(68, 72), unit(68, 76), LIMITS, NOW)
    assert g.cool_f == 74.0 and g.clamped
    assert g.violations == ["Cool moves at most 2°F per change: 74°F now, on the way to 72°F."]
    g = check(target(68, 80.5), unit(68, 76.3), LIMITS, NOW)
    assert g.cool_f == 78.0  # 76.3 + 2 = 78.3, rounded toward the current setpoint


def test_deadband_moves_the_setpoint_that_changed():
    # cool asked to drop to 70 with heat unchanged at 68: cool moves back up to keep 3°F
    g = check(target(68, 70), unit(68, 71), LIMITS, NOW)
    assert (g.heat_f, g.cool_f) == (68.0, 71.0)
    # heat asked up to 74 with cool unchanged at 76: heat moves back down
    g = check(target(74, 76), unit(72, 76), LIMITS, NOW)
    assert (g.heat_f, g.cool_f) == (73.0, 76.0)
    assert any("gap" in v for v in g.violations)


def test_deadband_uses_thermostat_min_delta():
    g = check(target(70, 74), unit(70, 74, settings={"heatCoolMinDelta": 50}), LIMITS, NOW)  # tenths -> 5°F
    assert g.cool_f - g.heat_f >= 5.0


def test_rate_limit_blocks():
    g = check(target(68, 75), unit(last_change_at=NOW - timedelta(minutes=10)), LIMITS, NOW)
    assert not g.ok and "10 min ago" in g.blocked_reason
    assert g.cool_f == 75.0  # still shows what would be written
    assert check(target(68, 75), unit(last_change_at=NOW - timedelta(minutes=31)), LIMITS, NOW).ok
    assert check(target(68, 75), unit(last_change_at=NOW - timedelta(minutes=10)), LIMITS, NOW,
                 enforce_rate_limit=False).ok


def person(until: datetime | None, by: str = "thermostat") -> PersonHold:
    return PersonHold(since=NOW - timedelta(minutes=50), first_seen=NOW - timedelta(minutes=40), by=by,  # type: ignore[arg-type]
                      until=until, heat_f=70.0, cool_f=74.0)


def test_person_hold_blocks_until_it_ends():
    g = check(target(68, 75), unit(person_hold=person(NOW + timedelta(hours=2))), LIMITS, NOW, tz=TZ)
    assert not g.ok
    assert g.blocked_reason == ("On your hold since 1:10 PM, until 4:00 PM; the controller waits until it ends or "
                                "you choose Back to automatic.")
    g = check(target(68, 75), unit(person_hold=person(None)), LIMITS, NOW, tz=TZ)
    assert g.blocked_reason == ("On your hold since 1:10 PM (until you change it); the controller waits until it "
                                "ends or you choose Back to automatic.")
    g = check(target(68, 75), unit(person_hold=person(NOW + timedelta(hours=1), by="app")), LIMITS, NOW, tz=TZ)
    assert g.blocked_reason.startswith("On your hold from the app since 1:10 PM, until 3:00 PM;")
    # a timed hold whose end has passed no longer blocks; the owner's own actions never do
    assert check(target(68, 75), unit(person_hold=person(NOW - timedelta(minutes=1))), LIMITS, NOW).ok
    assert check(target(68, 75), unit(person_hold=person(None)), LIMITS, NOW, enforce_manual_backoff=False).ok


def test_resume_backoff_blocks():
    backing = unit(resume_backoff_until=NOW + timedelta(hours=2), resume_seen_at=NOW - timedelta(hours=2))
    g = check(target(68, 75), backing, LIMITS, NOW, tz=TZ)
    assert not g.ok and g.blocked_reason == (
        "Resume was pressed at 12:00 PM: the upstairs thermostat follows the ecobee schedule until 4:00 PM, or "
        "until you choose Back to automatic.")
    assert check(target(68, 75), unit(resume_backoff_until=NOW - timedelta(minutes=1)), LIMITS, NOW).ok
    assert check(target(68, 75), backing, LIMITS, NOW, enforce_manual_backoff=False).ok


def test_unrecognised_event_blocks_everyone():
    odd = unit(hold=HoldInfo(heat_f=66.0, cool_f=80.0, hold_type="today"))
    for kw in ({}, {"enforce_manual_backoff": False, "enforce_rate_limit": False}):
        g = check(target(68, 75), odd, LIMITS, NOW, **kw)
        assert not g.ok and g.blocked_reason == (
            "An unrecognised ecobee event (today) is running on the upstairs thermostat; the app is hands-off "
            "until it ends.")
    # known kinds are not "unrecognised"
    assert check(target(68, 75), unit(hold=HoldInfo(heat_f=66.0, cool_f=80.0, hold_type="autoAway")), LIMITS, NOW).ok


def test_demand_response_tells_the_owner_to_skip_first():
    dr = unit(hold=HoldInfo(cool_offset_f=2.0, is_relative=True, hold_type="demandResponse"))
    g = check(target(68, 75), dr, LIMITS, NOW)
    assert not g.ok and "demand-response event" in g.blocked_reason and "Skip" not in g.blocked_reason
    g = check(target(68, 75), dr, LIMITS, NOW, enforce_manual_backoff=False, enforce_rate_limit=False,
              enforce_step=False, enforce_humidity=False)
    assert not g.ok and g.blocked_reason.endswith("Skip the event first.")


def event(**kw) -> UtilityEventState:
    base = {"id": 1, "unit_key": "up", "event_key": "link:a", "name": "Peak", "status": "announced",
            "start_at": NOW - timedelta(minutes=5), "end_at": NOW + timedelta(hours=3)}
    return UtilityEventState(**{**base, **kw})


def test_utility_event_blocks_by_its_clock_window_when_the_snapshot_lags():
    g = check(target(68, 75), unit(), LIMITS, NOW, events=[event()], tz=TZ)
    assert not g.ok and g.blocked_reason == (
        "The utility event 'Peak' is on for the upstairs thermostat until 5:00 PM; the controller stands aside "
        "while it runs.")
    g = check(target(68, 75), unit(), LIMITS, NOW, events=[event()], enforce_manual_backoff=False)
    assert not g.ok and g.blocked_reason.endswith("Skip the event first.")
    assert check(target(68, 75), unit(), LIMITS, NOW, events=[event(unit_key="main")]).ok  # another unit's
    assert check(target(68, 75), unit(), LIMITS, NOW, events=[event(skip="done")]).ok  # opted out
    assert check(target(68, 75), unit(), LIMITS, NOW, events=[event(start_at=NOW + timedelta(minutes=5))]).ok
    assert check(target(68, 75), unit(), LIMITS, NOW, events=[event(status="ended")]).ok


def test_owner_options_keep_only_the_hard_envelope():
    owner = {"enforce_rate_limit": False, "enforce_manual_backoff": False, "enforce_step": False,
             "enforce_humidity": False}
    humid = unit(68, 74, humidity=70.0, last_change_at=NOW - timedelta(minutes=5))
    g = check(target(68, 80), humid, LIMITS, NOW, **owner)  # no step limit, no humidity guard
    assert g.ok and (g.heat_f, g.cool_f) == (68.0, 80.0) and not g.clamped and g.violations == []
    g = check(target(58.2, 86), humid, LIMITS, NOW, **owner)  # min/max and the grid still apply
    assert g.ok and (g.heat_f, g.cool_f) == (60.0, 84.0) and g.clamped
    g = check(target(70, 71), humid, LIMITS, NOW, **owner)  # and the deadband
    assert g.ok and g.cool_f - g.heat_f >= LIMITS.min_deadband_f
    # the controller's defaults still step and guard humidity
    g = check(target(68, 80), unit(68, 74, humidity=45.0), LIMITS, NOW)
    assert g.cool_f == 76.0


def test_humidity_guard_never_raises_cooling():
    humid = unit(68, 76, humidity=62.0)
    g = check(target(68, 78), humid, LIMITS, NOW)
    assert g.cool_f == 76.0
    assert not g.ok and "humidity" in g.blocked_reason.lower()  # nothing else would change
    # a change that also lowers heat still goes through, without raising cool
    g = check(target(66, 78), humid, LIMITS, NOW)
    assert g.ok and (g.heat_f, g.cool_f) == (66.0, 76.0)
    assert any("humidity" in v.lower() for v in g.violations)
    # lowering cool is always fine when humid
    assert check(target(68, 75), humid, LIMITS, NOW).ok


def test_stale_disconnected_or_missing_snapshot_blocks():
    assert "min old" in check(target(68, 75), unit(age_min=20), LIMITS, NOW).blocked_reason
    assert "disconnected" in check(target(68, 75), unit(connected=False), LIMITS, NOW).blocked_reason
    g = check(target(68, 75), UnitStatus(unit_key="up", name="Upstairs"), LIMITS, NOW)
    assert not g.ok and "No live data" in g.blocked_reason


def test_mode_off_and_act_units():
    assert check(target(68, 75), unit(), LIMITS, NOW, mode="off").blocked_reason == "The controller is off."
    g = check(target(68, 75), unit(), LIMITS, NOW, mode="act", act_units=["main"])
    assert not g.ok and "suggest-only" in g.blocked_reason
    assert check(target(68, 75), unit(), LIMITS, NOW, mode="act", act_units=["up"]).ok
    assert check(target(68, 75), unit(), LIMITS, NOW, mode="suggest", act_units=["main"]).ok


def test_validate_policy_params_types_and_ranges():
    assert validate_policy_params({}, actor="owner") == []
    assert validate_policy_params({"linked_offset_f": 1.5}, actor="owner") == []
    assert "Unknown parameter" in validate_policy_params({"nope": 1}, actor="owner")[0]
    assert "number" in validate_policy_params({"linked_offset_f": "2"}, actor="owner")[0]
    assert "number" in validate_policy_params({"linked_offset_f": True}, actor="owner")[0]
    assert "true or false" in validate_policy_params({"precool_enabled": 1}, actor="owner")[0]
    assert "whole number" in validate_policy_params({"recovery_lead_min": 12.5}, actor="owner")[0]
    assert validate_policy_params({"linked_offset_f": 5.0}, actor="owner")  # field max is 4
    assert validate_policy_params("x", actor="owner")  # type: ignore[arg-type]


def test_validate_policy_params_claude_limits():
    # owner may go to the field limit; Claude only inside its sign-off range
    assert validate_policy_params({"linked_offset_f": 3.5}, actor="owner") == []
    assert "sign-off range" in validate_policy_params({"linked_offset_f": 3.5}, actor="claude")[0]
    assert "only the owner" in validate_policy_params({"precool_enabled": True}, actor="claude")[0]
    assert validate_policy_params({"recovery_lead_min": 30, "setback_gap_f": 2.5}, actor="claude") == []
    assert within_signoff_ranges({"recovery_lead_min": 30})
    assert not within_signoff_ranges({"recovery_lead_min": 50})
    assert not within_signoff_ranges({"bed_wing_independent": False})
    assert not within_signoff_ranges({})

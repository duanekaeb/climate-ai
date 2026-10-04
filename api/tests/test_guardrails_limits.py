"""guardrails.check: the hard limits are applied LAST and always win, protected ecobee events
and HomeKit-only data block, and a property test over random inputs."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from climate.control.guardrails import check
from climate.control.policy import UnitStatus, UnitTarget
from climate.sources.base import HoldInfo, UnitSnapshot
from climate.store.app_settings import HardLimits

NOW = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
LIMITS = HardLimits()


def unit(heat: float, cool: float, *, humidity: float = 45.0, hvac: str = "cool", settings: dict | None = None,
         hold: HoldInfo | None = None, source: str = "simulator") -> UnitStatus:
    snap = UnitSnapshot(unit_key="up", ts=NOW - timedelta(minutes=1), source=source, hvac_mode=hvac,  # type: ignore[arg-type]
                        heat_sp_f=heat, cool_sp_f=cool, zone_humidity=humidity, settings=settings or {}, hold=hold)
    return UnitStatus(unit_key="up", name="Upstairs", snapshot=snap, age_s=60)


def target(heat: float, cool: float) -> UnitTarget:
    return UnitTarget(unit_key="up", heat_f=heat, cool_f=cool, rule="comfort", reason="test")


def on_grid(x: float) -> bool:
    return abs(x * 2 - round(x * 2)) < 1e-9


def test_hard_minimum_wins_over_the_step_limit():
    # the current heat setpoint sits 10°F under the minimum: two steps cannot reach it, the limit wins
    g = check(target(68, 77), unit(50.0, 78.0), LIMITS, NOW, mode="act", act_units=["up"])
    assert g.ok and (g.heat_f, g.cool_f) == (60.0, 77.0)
    assert any("60°F minimum wins over the 2°F step limit" in v for v in g.violations)


def test_hard_maximum_wins_over_the_step_limit():
    g = check(target(68, 77), unit(66.0, 90.0), LIMITS, NOW)
    assert g.ok and g.cool_f == 84.0  # stepping 90 -> 88 would still be above the 84°F maximum
    assert any("84°F maximum wins" in v for v in g.violations)


def test_hard_minimum_wins_over_the_humidity_guard():
    # humid, and the current cool setpoint is below the hard minimum: raise it to the minimum anyway
    g = check(target(66, 74), unit(64.0, 68.0, humidity=70.0), LIMITS, NOW)
    assert g.ok and g.cool_f == 70.0 and g.cool_f - g.heat_f >= LIMITS.min_deadband_f


def test_off_grid_limits_tighten_onto_the_grid():
    lim = HardLimits(min_heat_f=60.3, max_heat_f=73.8, min_cool_f=70.2, max_cool_f=83.9)
    g = check(target(55, 90), unit(61.0, 83.0), lim, NOW)
    assert g.ok and (g.heat_f, g.cool_f) == (60.5, 83.5)


def test_impossible_limits_block():
    lim = HardLimits(min_heat_f=70.0, max_heat_f=65.0)
    g = check(target(68, 77), unit(68.0, 77.0), lim, NOW)
    assert not g.ok and "minimum above maximum" in g.blocked_reason


def test_vacation_and_demand_response_block():
    for event, words in (("vacation", "vacation event"), ("demandResponse", "demand-response event")):
        hold = HoldInfo(kind="temperature", heat_f=60.0, cool_f=84.0, hold_type=event)
        g = check(target(68, 77), unit(60.0, 84.0, hold=hold), LIMITS, NOW)
        assert not g.ok and words in g.blocked_reason and "never overrides" in g.blocked_reason
        # owners are blocked too (enforce flags only drop the rate limit and back-off)
        g = check(target(68, 77), unit(60.0, 84.0, hold=hold), LIMITS, NOW, enforce_rate_limit=False,
                  enforce_manual_backoff=False)
        assert not g.ok
    # Smart Away is ecobee's own event: the controller may override it
    away = HoldInfo(kind="climate", climate_ref="away", heat_f=62.0, cool_f=80.0, hold_type="autoAway")
    assert check(target(64, 78), unit(62.0, 80.0, hold=away), LIMITS, NOW).ok


def test_homekit_snapshot_needs_the_fallback():
    u = unit(68.0, 77.0, source="homekit")
    g = check(target(68, 76), u, LIMITS, NOW)
    assert not g.ok and "HomeKit" in g.blocked_reason
    assert check(target(68, 76), u, LIMITS, NOW, homekit_data_ok=True).ok


def test_property_ok_results_are_inside_limits_on_grid_and_keep_the_deadband():
    """Random current setpoints, targets and limits (some off the 0.5°F grid, some impossible):
    every ok result is inside the hard limits, on the grid and keeps the deadband. When the
    current setpoints are themselves valid, the step limit and the humidity guard hold too."""
    rng = random.Random(20261004)
    n_ok = n_valid_current = 0
    for _ in range(30000):
        min_heat = rng.choice([rng.uniform(50, 70), round(rng.uniform(50, 70) * 2) / 2])
        min_cool = rng.uniform(60, 80)
        lim = HardLimits(
            min_heat_f=min_heat, max_heat_f=min_heat + rng.uniform(-1, 16),
            min_cool_f=min_cool, max_cool_f=min_cool + rng.uniform(-1, 16),
            min_deadband_f=rng.choice([1.0, 2.0, 3.0, 3.3, 5.0]), max_step_f=rng.choice([0.5, 1.0, 2.0, 2.5, 4.0]),
        )
        cur_heat, cur_cool = rng.uniform(45, 85), rng.uniform(55, 95)
        if rng.random() < 0.5:
            cur_heat, cur_cool = round(cur_heat * 2) / 2, round(cur_cool * 2) / 2
        delta = rng.choice([None, 30, 40, 50, 3.5])  # heatCoolMinDelta: tenths above 20, else °F
        humidity = rng.choice([40.0, 70.0])
        u = unit(cur_heat, cur_cool, humidity=humidity, hvac=rng.choice(["heat", "cool", "auto", "auxHeatOnly"]),
                 settings={"heatCoolMinDelta": delta} if delta else {})
        g = check(target(rng.uniform(40, 90), rng.uniform(50, 100)), u, lim, NOW)
        if not g.ok:
            continue
        n_ok += 1
        deadband = max(lim.min_deadband_f, (delta / 10 if delta > 20 else delta) if delta else 0.0)
        assert lim.min_heat_f <= g.heat_f <= lim.max_heat_f, (lim, cur_heat, g)
        assert lim.min_cool_f <= g.cool_f <= lim.max_cool_f, (lim, cur_cool, g)
        assert on_grid(g.heat_f) and on_grid(g.cool_f), g
        assert g.cool_f - g.heat_f >= deadband - 1e-6, (deadband, g)
        valid_current = (
            lim.min_heat_f <= cur_heat <= lim.max_heat_f and lim.min_cool_f <= cur_cool <= lim.max_cool_f
            and cur_cool - cur_heat >= deadband and on_grid(cur_heat) and on_grid(cur_cool)
        )
        if valid_current:
            n_valid_current += 1
            assert abs(g.heat_f - cur_heat) <= lim.max_step_f + 1e-9, (lim.max_step_f, cur_heat, g)
            assert abs(g.cool_f - cur_cool) <= lim.max_step_f + 1e-9, (lim.max_step_f, cur_cool, g)
            if humidity > lim.max_indoor_rh:
                assert g.cool_f <= cur_cool, (cur_cool, g)
    assert n_ok > 10000 and n_valid_current > 100  # the generator really exercises both regimes

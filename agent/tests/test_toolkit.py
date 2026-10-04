"""Toolkit summaries against MockTransport fixtures, and gated-tool error propagation."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from climate_agent.toolkit import TOOLS, POLICY_PARAM_TYPES, Toolkit, input_schema
from conftest import SAVINGS, SETTINGS, STATUS, change


def call(api: Any, name: str, args: dict[str, Any] | None = None, run_id: int | None = None):
    async def go():
        try:
            return await Toolkit(api, run_id=run_id).call(name, args or {})
        finally:
            await api.aclose()

    return asyncio.run(go())


def test_house_status_summary(make_api):
    api, router = make_api({("GET", "/api/status"): STATUS})
    res = call(api, "get_house_status")
    assert not res.is_error
    text = res.text
    assert "Main floor [main]: cool, 76.2°F" in text
    assert "until 15:30 (ours)" in text  # 20:30Z shown in the house's time zone (CDT)
    assert "Twins' Room no sensor (temp unknown)" in text
    assert "Toy Room* 77.2°F occupied" in text
    assert "maxed 45 min" in text
    assert "Weather data by Open-Meteo.com" in text
    assert "Upstairs at 100% duty" in text
    assert router.paths() == ["GET /api/status"]  # tz came from the status itself
    assert len(text) < 2000


def test_savings_with_interval(make_api):
    api, _ = make_api({("GET", "/api/analytics/savings"): SAVINGS})
    text = call(api, "savings_report").text
    assert "+7.1%" in text
    assert "90% interval [2.0%, 12.3%]" in text
    assert "NO SAVINGS CLAIM" not in text
    assert "main exp 1,400 min act 1,200 min (+14.3%)" in text


def test_savings_without_valid_baseline_makes_no_claim(make_api):
    bad = {**SAVINGS, "baseline_ok": False}
    api, _ = make_api({("GET", "/api/analytics/savings"): bad})
    assert "NO SAVINGS CLAIM: the baseline does not pass its checks." in call(api, "savings_report").text

    spans = {**SAVINGS, "ci90_low_pct": -3.0, "ci90_high_pct": 9.0}
    api, _ = make_api({("GET", "/api/analytics/savings"): spans})
    assert "includes zero" in call(api, "savings_report").text


def test_savings_passes_dates_as_query(make_api):
    api, router = make_api({("GET", "/api/analytics/savings"): SAVINGS})
    call(api, "savings_report", {"start": "2026-09-01", "end": "2026-09-14"})
    q = router.requests[0].url.params
    assert q["start"] == "2026-09-01" and q["end"] == "2026-09-14"


def test_pending_changes_only_mine_with_range_check(make_api):
    changes = [
        change(7, "claude", {"linked_offset_f": 1.5}),
        change(8, "claude", {"linked_offset_f": 3.5}),
        change(9, "owner", {"precool_enabled": True}),
        change(10, "nothing", {"setback_gap_f": 2.5}, status="active"),
    ]
    api, _ = make_api({("GET", "/api/changes"): changes, ("GET", "/api/control/settings"): SETTINGS})
    text = call(api, "review_pending_changes").text
    assert text.startswith("2 change(s) await your sign-off (1 more await the owner)")
    assert "#7" in text and "#8" in text and "#9" not in text and "#10" not in text
    line7 = next(line for line in text.splitlines() if "#7" in line)
    line8 = next(line for line in text.splitlines() if "#8" in line)
    assert "inside your sign-off ranges" in line7
    assert "OUTSIDE your sign-off ranges: linked_offset_f=3.5" in line8
    assert "-4.21" in line7  # gate results are shown


def test_no_pending_changes(make_api):
    api, router = make_api({("GET", "/api/changes"): [change(9, "owner", {"precool_enabled": True})]})
    assert call(api, "review_pending_changes").text == "No changes await your sign-off. 1 await the owner."
    assert router.paths() == ["GET /api/changes"]


def test_sign_off_posts_decision(make_api):
    decided = {**change(7, "nothing", {"linked_offset_f": 1.5}, status="trial"),
               "trial_start": "2026-10-04T05:00:00Z", "trial_end": "2026-10-11T05:00:00Z"}
    api, router = make_api({("POST", "/api/changes/7/decision"): decided, ("GET", "/api/control/settings"): SETTINGS})
    res = call(api, "sign_off_change", {"change_id": 7, "decision": "approve", "reason": "Backtest -4.2% [-6.9, -1.5]"})
    assert not res.is_error
    assert "Recorded approve on change #7" in res.text and "status now trial" in res.text
    assert router.bodies("POST", "/api/changes/7/decision") == [{"decision": "approve", "reason": "Backtest -4.2% [-6.9, -1.5]"}]


def test_gated_403_comes_back_as_tool_error(make_api):
    refused = httpx.Response(403, json={"detail": "Claude may not decide this change: linked_offset_f outside sign-off range"})
    api, _ = make_api({("POST", "/api/changes/8/decision"): refused})
    res = call(api, "sign_off_change", {"change_id": 8, "decision": "approve", "reason": "looks fine"})
    assert res.is_error
    assert "Refused (403)" in res.text and "outside sign-off range" in res.text


def test_gated_422_and_500_and_unreachable(make_api):
    api, _ = make_api({("POST", "/api/changes"): httpx.Response(422, json={"detail": [{"loc": ["body", "title"], "msg": "too short"}]})})
    res = call(api, "propose_policy_change", {"title": "abc", "rationale": "r", "params": {"linked_offset_f": 1.5}})
    assert res.is_error and "Rejected (422): title: too short" in res.text

    api, _ = make_api({("POST", "/api/reports"): httpx.Response(500, text="boom")})
    res = call(api, "publish_report", {"kind": "nightly", "title": "Nightly", "body_md": "ok"})
    assert res.is_error and "server error 500" in res.text

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    api, _ = make_api({("GET", "/api/analytics/drift"): down})
    res = call(api, "drift_report")
    assert res.is_error and "could not be reached" in res.text


def test_policy_params_precheck_blocks_unknown_names_without_a_request(make_api):
    api, router = make_api({})
    res = call(api, "propose_policy_change", {"title": "abc", "rationale": "r", "params": {"linked_ofset_f": 1.5}})
    assert res.is_error and "Unknown policy parameter(s) in params: linked_ofset_f" in res.text
    res = call(api, "run_backtest", {"params": {"linked_floors_enabled": "yes"}})
    assert res.is_error and "true or false" in res.text
    res = call(api, "run_backtest", {"params": {"recovery_lead_min": 12.5}})
    assert res.is_error and "whole number" in res.text
    assert router.requests == []


def test_invalid_arguments_are_tool_errors(make_api):
    api, router = make_api({})
    res = call(api, "sign_off_change", {"change_id": 7, "decision": "maybe", "reason": "x"})
    assert res.is_error and res.text.startswith("Invalid arguments for sign_off_change")
    res = call(api, "query_runtime", {"days": 500})
    assert res.is_error
    res = call(api, "query_runtime", {"surprise": 1})
    assert res.is_error
    assert router.requests == []


def test_propose_policy_and_experiment_bodies(make_api):
    created = {**change(11, "owner", {"linked_offset_f": 1.5}, status="backtest"), "proposed_by": "claude"}
    experiment = {"id": 4, "created_at": "2026-10-04T05:00:00Z", "name": "Offset switchback", "hypothesis": "h",
                  "metric": "house_runtime_weather_normalized", "arms": [], "n_days": 28, "block_days": 2,
                  "checkpoints": [{"day": 10, "info_fraction": 0.36, "alpha_spent": 0.01, "z_crit": 2.5}],
                  "alpha": 0.1, "status": "proposed", "proposed_by": "claude"}
    api, router = make_api({("POST", "/api/changes"): created, ("POST", "/api/experiments"): experiment})
    res = call(api, "propose_policy_change", {"title": "Main 1.5°F under", "rationale": "why", "params": {"linked_offset_f": 1.5, "recovery_lead_min": 30.0}})
    assert not res.is_error and "#11" in res.text
    assert router.bodies("POST", "/api/changes") == [
        {"title": "Main 1.5°F under", "rationale": "why", "params": {"linked_offset_f": 1.5, "recovery_lead_min": 30}}
    ]
    api, router = make_api({("POST", "/api/experiments"): experiment})
    res = call(api, "propose_experiment", {
        "name": "Offset switchback", "hypothesis": "1.5°F cuts upstairs runtime 10%",
        "arms": [{"key": "current", "label": "Current"}, {"key": "offset_15", "label": "1.5°F", "params": {"linked_offset_f": 1.5}}],
    })
    assert not res.is_error and "day 10" in res.text
    body = router.bodies("POST", "/api/experiments")[0]
    assert body["arms"][1]["params"] == {"linked_offset_f": 1.5} and body["arms"][0]["params"] == {}
    assert body["n_days"] == 28 and body["n_checkpoints"] == 3


def test_publish_report_carries_run_id(make_api):
    report = {"id": 55, "created_at": "2026-10-04T05:00:00Z", "kind": "nightly", "author": "claude", "title": "Nightly 10-03", "body_md": "x"}
    api, router = make_api({("POST", "/api/reports"): report})
    res = call(api, "publish_report", {"kind": "nightly", "title": "Nightly 10-03", "body_md": "All rooms in band.",
                                       "period_start": "2026-10-03", "period_end": "2026-10-03"}, run_id=42)
    assert not res.is_error and "#55" in res.text
    body = router.bodies("POST", "/api/reports")[0]
    assert body["agent_run_id"] == 42 and body["period_start"] == "2026-10-03" and body["data"] == {}


def test_weather_cites_open_meteo_and_runtime_rounds(make_api):
    weather = {"source": "open-meteo", "attribution": "Weather data by Open-Meteo.com", "points": [
        {"ts": "2026-10-03T18:00:00Z", "kind": "observed", "temp_f": 88.44, "cloud_cover": 20.0, "shortwave_wm2": 700.0},
        {"ts": "2026-10-04T18:00:00Z", "kind": "forecast", "temp_f": 93.2, "cloud_cover": 10.0, "shortwave_wm2": 820.0},
    ]}
    runtime = [
        {"Date": "2026-10-02", "unit_key": "up", "cool_min": 300.4, "heat_min": 0.0, "aux_min": 0.0, "fan_min": 10.0,
         "expected_min": 280.0, "mode": "cool", "outdoor_mean_f": 81.0, "outdoor_max_f": 93.0, "maxed_min": 45.0},
        {"Date": "2026-10-02", "unit_key": "main", "cool_min": 150.2, "heat_min": 0.0, "aux_min": 0.0, "fan_min": 10.0,
         "expected_min": 170.0, "mode": "cool", "outdoor_mean_f": 81.0, "outdoor_max_f": 93.0, "maxed_min": 0.0},
    ]
    api, _ = make_api({("GET", "/api/weather"): weather, ("GET", "/api/control/settings"): SETTINGS})
    w = call(api, "get_weather").text
    assert w.rstrip().endswith("Weather data by Open-Meteo.com") and "93°F" in w
    api, _ = make_api({("GET", "/api/runtime/daily"): runtime})
    r = call(api, "query_runtime", {"days": 3}).text
    assert "2026-10-02 cool: 451 min (exp 450 min)" in r
    assert "Upstairs: 300 min total, maxed 45 min; 300 min vs 280 min expected on 1 baseline days (+7.3%)" in r
    assert "not a savings claim" in r


def test_room_without_sensor_stays_unknown(make_api):
    history = {"room_key": "twins_room", "hours": 24, "setpoints": [],
               "points": [{"ts": "2026-10-03T03:00:00Z", "temp_f": None, "occupied": True, "state": "asleep"}]}
    api, _ = make_api({("GET", "/api/rooms/twins_room/history"): history, ("GET", "/api/control/settings"): SETTINGS})
    text = call(api, "query_room", {"room_key": "twins_room"}).text
    assert "Temperature: unknown" in text and "Do not estimate it" in text


def test_every_tool_has_a_self_contained_schema():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))
    for spec in TOOLS:
        schema = input_schema(spec.name)
        assert schema["type"] == "object" and "$ref" not in str(schema) and "$defs" not in schema
        assert spec.description
    assert set(POLICY_PARAM_TYPES) >= {"linked_offset_f", "recovery_lead_min", "precool_enabled"}
    required = {"get_house_status", "query_runtime", "query_room", "get_weather", "baseline_report", "savings_report",
                "waterfall_report", "coupling_report", "comfort_report", "drift_report", "natural_experiment_report",
                "list_actions", "explain_action", "list_reports", "get_settings", "run_backtest", "simulate_plan",
                "estimate_power", "request_refit", "review_pending_changes", "sign_off_change",
                "propose_policy_change", "propose_experiment", "publish_report"}
    assert required <= set(names)
    assert not any("thermostat" in n or n.startswith(("set_", "hold", "resume")) for n in names)

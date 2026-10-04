"""Reports and alerts, experiments (route order, proposer mapping, owner-only decision),
models / refit jobs / backtest validation, and analytics defaults. Services are monkeypatched."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from climate.analytics import attribution, baseline
from climate.api.schemas import (
    BacktestOut,
    ExperimentAnalysis,
    PowerOut,
    Savings,
    Waterfall,
)
from climate.experiments import analysis, switchback
from climate.models import backtest
from climate.store.db import session_scope
from climate.store.orm import AgentRun, Alert, Experiment, ModelFit
from climate.timeutil import local_date, utcnow

ARMS = [{"key": "a", "label": "Offset 1°F", "params": {"linked_offset_f": 1.0}},
        {"key": "b", "label": "Offset 2°F", "params": {"linked_offset_f": 2.0}}]
DESIGN = {"n_days": 28, "block_days": 2, "seed": 7, "alpha": 0.1,
          "checkpoints": [{"day": 14, "info_fraction": 0.5, "alpha_spent": 0.006, "z_crit": 2.75},
                          {"day": 28, "info_fraction": 1.0, "alpha_spent": 0.1, "z_crit": 1.68}]}


def _add(row) -> int:
    with session_scope() as s:
        s.add(row)
        s.flush()
        return row.id


# --- reports / alerts --------------------------------------------------------------------


def test_reports_authorship(owner, agent):
    run_id = _add(AgentRun(kind="nightly", status="running", requested_by="schedule"))
    r = agent.post("/api/reports", json={"kind": "nightly", "title": "Nightly review", "body_md": "# ok",
                                         "agent_run_id": run_id, "period_start": "2026-10-02", "period_end": "2026-10-03"})
    assert r.status_code == 200, r.text
    assert (r.json()["author"], r.json()["kind"], r.json()["agent_run_id"]) == ("claude", "nightly", run_id)

    assert owner.post("/api/reports", json={"kind": "nightly", "title": "Fake", "body_md": "x"}).status_code == 422
    note = owner.post("/api/reports", json={"kind": "note", "title": "Filter changed", "body_md": "New MERV 11."})
    assert note.status_code == 200 and note.json()["author"] == "system"

    bad = agent.post("/api/reports", json={"kind": "note", "title": "Bad", "body_md": "x", "agent_run_id": 9999,
                                           "period_start": "2026-10-05", "period_end": "2026-10-01"})
    assert bad.status_code == 422 and len(bad.json()["detail"]) == 2

    assert [x["kind"] for x in owner.get("/api/reports").json()] == ["note", "nightly"]
    assert [x["kind"] for x in owner.get("/api/reports", params={"kind": "nightly"}).json()] == ["nightly"]
    assert owner.get(f"/api/reports/{r.json()['id']}").json()["body_md"] == "# ok"
    assert owner.get("/api/reports/9999").status_code == 404


def test_alerts_list_and_resolve(owner):
    a = _add(Alert(level="warn", kind="sensor_offline", title="Office sensor offline", body="No reading for 1 h"))
    _add(Alert(level="info", kind="note", title="Old", body="", resolved_at=utcnow()))
    assert [x["id"] for x in owner.get("/api/alerts").json()] == [a]
    r = owner.post(f"/api/alerts/{a}/resolve")
    assert r.status_code == 200 and r.json()["resolved_at"] is not None
    assert owner.get("/api/alerts").json() == []
    assert len(owner.get("/api/alerts", params={"open": "false"}).json()) == 2
    assert owner.post(f"/api/alerts/{a}/resolve").status_code == 409
    assert owner.post("/api/alerts/9999/resolve").status_code == 404


# --- experiments -------------------------------------------------------------------------


def _experiment(status="proposed") -> int:
    return _add(Experiment(name="Offset switchback", hypothesis="2°F saves runtime", arms=ARMS, design=DESIGN,
                           status=status, proposed_by="claude"))


def test_power_route_is_not_swallowed_by_the_id_route(owner, monkeypatch):
    seen = {}

    def fake_power(session, effect_pct, alpha=0.1, power_=0.8, block_days=2):
        seen.update(effect_pct=effect_pct, alpha=alpha, power=power_)
        return PowerOut(effect_pct=effect_pct, alpha=alpha, power=power_, resid_cv=0.2, days_per_arm=20,
                        total_days=40, note="ok")

    monkeypatch.setattr(analysis, "power", fake_power)
    r = owner.get("/api/experiments/power", params={"effect_pct": 15})
    assert r.status_code == 200, r.text
    assert seen == {"effect_pct": 15.0, "alpha": 0.1, "power": 0.8}
    assert owner.get("/api/experiments/power", params={"effect_pct": 0}).status_code == 422


def test_experiment_list_and_detail(owner, monkeypatch):
    eid = _experiment()
    monkeypatch.setattr(analysis, "analyze", lambda session, exp: ExperimentAnalysis(
        days_observed=0, effect_pct=None, ci_low_pct=None, ci_high_pct=None, checkpoint_reached=None,
        decision="not_started", note="Not started."))
    listed = owner.get("/api/experiments").json()
    assert listed[0]["id"] == eid and listed[0]["n_days"] == 28 and len(listed[0]["checkpoints"]) == 2
    detail = owner.get(f"/api/experiments/{eid}").json()
    assert detail["experiment"]["arms"][1]["params"] == {"linked_offset_f": 2.0}
    assert detail["analysis"]["decision"] == "not_started" and detail["schedule"] == []
    assert owner.get("/api/experiments/9999").status_code == 404


@pytest.mark.parametrize(("who", "expected"), [("agent", "claude"), ("owner", "owner")])
def test_propose_experiment_maps_proposer(request, monkeypatch, who, expected):
    client = request.getfixturevalue(who)
    seen = {}

    def fake_create(session, body, proposed_by):
        seen["by"] = proposed_by
        e = Experiment(name=body.name, hypothesis=body.hypothesis, arms=[a.model_dump() for a in body.arms],
                       design={"n_days": body.n_days, "block_days": body.block_days, "seed": 1, "alpha": body.alpha,
                               "checkpoints": []}, status="proposed", proposed_by=proposed_by)
        session.add(e)
        session.flush()
        return e

    monkeypatch.setattr(switchback, "create_experiment", fake_create)
    body = {"name": "Offset test", "hypothesis": "A wider offset saves runtime", "arms": ARMS}
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 200, r.text
    assert seen["by"] == expected and r.json()["proposed_by"] == expected
    dup = dict(body, arms=[ARMS[0], ARMS[0]])
    assert client.post("/api/experiments", json=dup).status_code == 422
    unknown = dict(body, arms=[ARMS[0], {"key": "b", "label": "B", "params": {"warp_drive": 1}}])
    assert client.post("/api/experiments", json=unknown).status_code == 422


def test_experiment_decision_owner_only(owner, monkeypatch):
    eid = _experiment()

    def fake_decide(session, experiment_id, decision, reason):
        e = session.get(Experiment, experiment_id)
        if e.status != "proposed":
            raise ValueError("Only a proposed experiment can be approved.")
        e.status, e.start_date = "approved", local_date(utcnow(), "America/Chicago") + timedelta(days=1)
        return e

    monkeypatch.setattr(switchback, "decide_experiment", fake_decide)
    r = owner.post(f"/api/experiments/{eid}/decision", json={"decision": "approve", "reason": "go ahead"})
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["start_date"]
    again = owner.post(f"/api/experiments/{eid}/decision", json={"decision": "approve", "reason": "again"})
    assert again.status_code == 409
    assert owner.post("/api/experiments/9999/decision", json={"decision": "stop", "reason": "nope"}).status_code == 404


# --- models / jobs -----------------------------------------------------------------------


def test_models_latest_per_kind_unit_mode(owner):
    old = _add(ModelFit(kind="baseline", unit_key="main", mode="cool", params={}, status="retired"))
    new = _add(ModelFit(kind="baseline", unit_key="main", mode="cool", params={"v": 2}, status="active"))
    rc = _add(ModelFit(kind="rc", unit_key=None, mode="cool", params={}, status="candidate"))
    ids = {m["id"] for m in owner.get("/api/models").json()}
    assert ids == {new, rc} and old not in ids


def test_refit_queues_one_job(owner, agent):
    first = agent.post("/api/models/refit")
    assert first.status_code == 200 and first.json()["status"] == "queued" and first.json()["kind"] == "refit"
    second = owner.post("/api/models/refit")
    assert second.json()["id"] == first.json()["id"]
    assert owner.get(f"/api/jobs/{first.json()['id']}").json()["status"] == "queued"
    assert owner.get("/api/jobs/9999").status_code == 404


def test_backtest_validates_params(agent, monkeypatch):
    seen = {}

    def fake_backtest(session, params, days=28):
        seen.update(params=params, days=days)
        return BacktestOut(days=days, model="rule_of_thumb", current_runtime_min=100, candidate_runtime_min=95,
                           delta_pct=-5, ci90_pct=(-9, -1), comfort_violation_min_current=0,
                           comfort_violation_min_candidate=0, beats_model_uncertainty=False, note="ok")

    monkeypatch.setattr(backtest, "backtest", fake_backtest)
    assert agent.post("/api/models/backtest", json={"params": {"warp_drive": 1}}).status_code == 422
    out_of_range = agent.post("/api/models/backtest", json={"params": {"linked_offset_f": 10}})
    assert out_of_range.status_code == 422 and "linked_offset_f" in str(out_of_range.json())
    r = agent.post("/api/models/backtest", json={"params": {"linked_offset_f": 2.0}, "days": 14})
    assert r.status_code == 200 and seen == {"params": {"linked_offset_f": 2.0}, "days": 14}
    assert agent.post("/api/models/backtest", json={"days": 400}).status_code == 422


# --- analytics -----------------------------------------------------------------------------


def test_savings_and_waterfall_defaults(owner, monkeypatch):
    seen = {}

    def fake_savings(session, start, end):
        seen["savings"] = (start, end)
        return Savings(start=start, end=end, n_days=(end - start).days + 1, expected_min=None, actual_min=0,
                       savings_min=None, savings_pct=None, ci90_low_pct=None, ci90_high_pct=None, by_unit=[],
                       days=[], baseline_ok=False, note="Baselines are not ready yet.")

    def fake_waterfall(session, week_start):
        seen["waterfall"] = week_start
        return Waterfall(week_start=week_start, prev_week_start=week_start - timedelta(days=7), items=[], note="")

    monkeypatch.setattr(attribution, "savings", fake_savings)
    monkeypatch.setattr(attribution, "waterfall", fake_waterfall)
    today = local_date(utcnow(), "America/Chicago")
    assert owner.get("/api/analytics/savings").json()["n_days"] == 14
    assert seen["savings"] == (today - timedelta(days=14), today - timedelta(days=1))
    assert owner.get("/api/analytics/savings", params={"start": "2026-09-10", "end": "2026-09-01"}).status_code == 422

    owner.get("/api/analytics/waterfall")
    ws = seen["waterfall"]
    assert ws.weekday() == 0 and timedelta(days=7) <= today - ws < timedelta(days=14)
    owner.get("/api/analytics/waterfall", params={"week_start": "2026-09-16"})  # a Wednesday
    assert seen["waterfall"] == date(2026, 9, 14)


def test_baselines_from_model_fits(owner, monkeypatch):
    params = {"balance_point_f": 64.0, "intercept_s": 600.0, "slope_s_per_dd": 1800.0, "n_days": 45, "r2": 0.91,
              "cvrmse": 0.12, "nmbe": 0.001, "resid_std_s": 900.0, "resid_lag1": 0.3}
    _add(ModelFit(kind="baseline", unit_key="up", mode="cool", params=params, status="active",
                  train_start=date(2026, 7, 1), train_end=date(2026, 9, 28)))
    _add(ModelFit(kind="baseline", unit_key="main", mode="cool", params=dict(params, cvrmse=0.31), status="active",
                  train_start=date(2026, 7, 1), train_end=date(2026, 9, 28)))
    _add(ModelFit(kind="baseline", unit_key="bed", mode="cool", params=params, status="retired",
                  train_start=date(2026, 7, 1), train_end=date(2026, 9, 28)))

    def broken(session):
        raise RuntimeError("baseline module not ready")

    monkeypatch.setattr(baseline, "active_fits", broken)  # falls back to the stored JSON
    rows = owner.get("/api/analytics/baselines").json()
    assert [(b["unit_key"], b["passes"]) for b in rows] == [("main", False), ("up", True)]
    up = rows[1]
    assert (up["intercept_min"], up["slope_min_per_dd"], up["n_days"]) == (10.0, 30.0, 45)

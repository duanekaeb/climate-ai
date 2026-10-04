"""The change gates: validation, permissions, backtest -> shadow -> sign-off -> trial -> active."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from climate.api.schemas import BacktestOut
from climate.control import changes
from climate.store.orm import Change, PolicyVersion
from climate.timeutil import to_local, utcnow

TZ = "America/Chicago"


def fake_backtest(beats: bool = True):
    def _bt(session, params, days=28):
        return BacktestOut(days=days, model="rule_of_thumb", current_runtime_min=1000.0, candidate_runtime_min=880.0,
                           delta_pct=-12.0, ci90_pct=(-18.0, -6.0), comfort_violation_min_current=0.0,
                           comfort_violation_min_candidate=0.0, beats_model_uncertainty=beats,
                           note="12% less runtime" if beats else "within the model's noise")
    return _bt


@pytest.fixture
def backtest_ok(monkeypatch):
    import climate.models.backtest as bt

    monkeypatch.setattr(bt, "backtest", fake_backtest(True))


def awaiting(db, proposed_by: str, params: dict) -> Change:
    c = changes.propose_policy_change(db, proposed_by, "Tune the policy", "because", params)  # type: ignore[arg-type]
    assert c.status == "backtest", c.gates
    c.status = "awaiting_signoff"
    db.flush()
    return c


def test_validation_on_propose(db):
    ok = changes.propose_policy_change(db, "model", "Wider offset", "r", {"linked_offset_f": 1.5})
    assert ok.status == "backtest" and ok.gates["validation"] == []
    assert ok.payload["params"] == {"linked_offset_f": 1.5} and ok.payload["current"] == {"linked_offset_f": 1.0}
    bad = changes.propose_policy_change(db, "claude", "Too far", "r", {"linked_offset_f": 3.5})
    assert bad.status == "rejected" and "sign-off range" in bad.gates["validation"][0]
    owner_only = changes.propose_policy_change(db, "claude", "Switch", "r", {"precool_enabled": True})
    assert owner_only.status == "rejected"
    assert changes.propose_policy_change(db, "owner", "Switch", "r", {"precool_enabled": True}).status == "backtest"
    assert changes.propose_policy_change(db, "owner", "Nothing", "r", {}).status == "rejected"
    same = changes.propose_policy_change(db, "owner", "Same", "r", {"linked_offset_f": 1.0})
    assert same.status == "rejected" and "already matches" in same.gates["validation"][0]
    assert changes.needs(bad) == "nothing"


def test_permission_matrix(db):
    in_range = {"linked_offset_f": 1.5}
    out_range = {"linked_offset_f": 3.5}

    model_in = awaiting(db, "model", in_range)
    assert changes.needs(model_in) == "claude"
    with pytest.raises(PermissionError):
        changes.decide(db, model_in.id, "claude", "reject", "veto")
    assert changes.decide(db, model_in.id, "claude", "hold", "need a hotter week").status == "held"
    assert changes.needs(model_in) == "owner"
    assert changes.decide(db, model_in.id, "owner", "reject", "not now").status == "rejected"

    model_out = awaiting(db, "model", out_range)
    assert changes.needs(model_out) == "owner"
    with pytest.raises(PermissionError):
        changes.decide(db, model_out.id, "claude", "approve", "looks fine")

    claude_in = awaiting(db, "claude", in_range)
    assert changes.needs(claude_in) == "owner"
    with pytest.raises(PermissionError):
        changes.decide(db, claude_in.id, "claude", "approve", "my own idea")

    # a hold the owner placed can't be lifted by Claude
    owner_held = awaiting(db, "model", in_range)
    changes.decide(db, owner_held.id, "owner", "hold", "not this month")
    with pytest.raises(PermissionError):
        changes.decide(db, owner_held.id, "claude", "approve", "data looks good")

    owner_in = awaiting(db, "owner", in_range)
    with pytest.raises(PermissionError):
        changes.decide(db, owner_in.id, "claude", "hold", "wait")
    assert changes.decide(db, owner_in.id, "owner", "hold", "wait for fall").status == "held"

    # the owner may approve the out-of-range model change; it starts a trial
    approved = changes.decide(db, model_out.id, "owner", "approve", "go")
    assert approved.status == "trial" and approved.decided_by == "owner"


def test_status_rules(db):
    c = changes.propose_policy_change(db, "model", "x", "r", {"recovery_lead_min": 30})
    with pytest.raises(ValueError, match="backtest"):
        changes.decide(db, c.id, "claude", "approve", "early")
    # the owner may skip the remaining gates, which is recorded
    done = changes.decide(db, c.id, "owner", "approve", "I know this house")
    assert done.status == "trial"
    assert done.gates["decision"]["skipped_gates"] == ["backtest", "shadow"]
    with pytest.raises(ValueError):
        changes.decide(db, c.id, "owner", "approve", "again")
    # only one trial at a time
    other = awaiting(db, "model", {"setback_gap_f": 3.0})
    with pytest.raises(ValueError, match="already in its trial"):
        changes.decide(db, other.id, "claude", "approve", "fine")
    rejected = changes.propose_policy_change(db, "claude", "bad", "r", {"nope": 1})
    with pytest.raises(ValueError):
        changes.decide(db, rejected.id, "owner", "approve", "force")
    with pytest.raises(ValueError):
        changes.decide(db, 999999, "owner", "approve", "missing")


def test_backtest_failure_rejects_and_errors_retry(db, monkeypatch):
    import climate.models.backtest as bt

    monkeypatch.setattr(bt, "backtest", fake_backtest(False))
    c = changes.propose_policy_change(db, "model", "x", "r", {"linked_offset_f": 1.5})
    assert changes.advance(db, utcnow()) == [c.id]
    assert c.status == "rejected" and "uncertainty" in c.decision_reason
    assert c.gates["backtest"]["beats_model_uncertainty"] is False

    def boom(*a, **k):
        raise RuntimeError("no baseline yet")

    monkeypatch.setattr(bt, "backtest", boom)
    c2 = changes.propose_policy_change(db, "model", "y", "r", {"linked_offset_f": 2.0})
    assert changes.advance(db, utcnow()) == []
    assert c2.status == "backtest" and "no baseline yet" in c2.gates["backtest"]["error"]


def test_full_lifecycle(db, backtest_ok):
    seed_active = db.execute(select(PolicyVersion).where(PolicyVersion.status == "active")).scalar_one()
    c = changes.propose_policy_change(db, "model", "Main floor 1.5°F under upstairs", "r", {"linked_offset_f": 1.5})
    t0 = utcnow()
    assert changes.advance(db, t0) == [c.id]
    assert c.status == "shadow" and c.gates["shadow"]["days"] == changes.SHADOW_DAYS
    assert c.gates["backtest"]["beats_model_uncertainty"] is True
    assert changes.advance(db, t0 + timedelta(days=1)) == []
    assert changes.advance(db, t0 + timedelta(days=3, minutes=1)) == [c.id]
    assert c.status == "awaiting_signoff" and changes.needs(c) == "claude"

    trial = changes.decide(db, c.id, "claude", "approve", "inside my range and it beat the model")
    assert trial.status == "trial" and trial.trial_end - trial.trial_start == timedelta(days=changes.TRIAL_DAYS)
    pv = db.get(PolicyVersion, trial.policy_version_id)
    assert pv.status == "trial" and pv.change_id == c.id and pv.params["linked_offset_f"] == 1.5
    assert pv.params["setback_gap_f"] == 2.0  # the rest of the active policy carried over

    # trial params only inside the afternoon window
    day = to_local(trial.trial_start, TZ).date() + timedelta(days=1)
    afternoon = datetime(day.year, day.month, day.day, 14, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)
    morning = afternoon - timedelta(hours=5)
    params, pid = changes.policy_for(db, afternoon, TZ)
    assert pid == pv.id and params.linked_offset_f == 1.5
    params, pid = changes.policy_for(db, morning, TZ)
    assert pid == seed_active.id and params.linked_offset_f == 1.0

    assert changes.advance(db, trial.trial_end + timedelta(minutes=1)) == [c.id]
    assert c.status == "active" and pv.status == "active"
    db.refresh(seed_active)
    assert seed_active.status == "retired"
    active, active_id = changes.active_policy(db)
    assert active_id == pv.id and active.linked_offset_f == 1.5
    assert "result" in c.gates["trial"] and c.gates["trial"]["result"]["regression"] is False


def test_trial_with_comfort_regression_is_rejected(db):
    c = awaiting(db, "owner", {"linked_offset_f": 0.5})
    changes.decide(db, c.id, "owner", "approve", "try it")
    start, end = c.trial_start, c.trial_end
    # occupied Toy Room afternoons: in band before the trial, 5°F over during it
    rows = []
    for d in range(-6, 7):
        local_day = to_local(start, TZ).date() + timedelta(days=d)
        for hour in (13, 15, 17):
            ts = datetime(local_day.year, local_day.month, local_day.day, hour, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)
            if not (start - (end - start) <= ts < end):
                continue
            temp = 83.0 if ts >= start else 75.0
            rows.append({"ts": ts, "temp": temp})
    for r in rows:
        db.execute(text("INSERT INTO readings_5m (ts, sensor_key, temp_f, occupied, source) "
                        "VALUES (:ts, 'up.toy_room', :temp, true, 'ecobee_report')"), r)
    assert changes.advance(db, end + timedelta(minutes=1)) == [c.id]
    assert c.status == "rejected" and "comfort regression" in c.decision_reason
    pv = db.get(PolicyVersion, c.policy_version_id)
    assert pv.status == "retired"
    result = c.gates["trial"]["result"]
    assert result["regression"] is True and result["before"]["in_band_pct"] == 100.0
    assert changes.active_policy(db)[0].linked_offset_f == 1.0

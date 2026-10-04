"""Switchback design, lifecycle, day filling, analysis decisions and power."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from scipy import stats
from sqlalchemy import select

from climate.api.schemas import ArmIn, ProposeExperimentBody
from climate.experiments import analysis, switchback
from climate.store.orm import Experiment, ExperimentDay
from climate.timeutil import local_date, utcnow

TZ = "America/Chicago"


# ---------------------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("n_days,k,alpha", [(28, 3, 0.10), (30, 3, 0.05), (21, 2, 0.10), (14, 1, 0.10), (60, 3, 0.2)])
def test_checkpoints_spend_exactly_alpha(n_days, k, alpha):
    cps = switchback.plan_checkpoints(n_days, k, alpha)
    assert len(cps) == k
    assert cps[-1].day == n_days and cps[-1].info_fraction == pytest.approx(1.0)
    assert sum(c.alpha_spent for c in cps) == pytest.approx(alpha, abs=1e-12)
    assert all(c.alpha_spent > 0 for c in cps)
    assert [c.day for c in cps] == sorted({c.day for c in cps})
    z = [c.z_crit for c in cps]
    assert all(a > b for a, b in zip(z, z[1:]))  # stricter (wider) early
    if k == 1:
        assert z[0] == pytest.approx(stats.norm.isf(alpha / 2))
    # the exact correlated-looks computation leaves no alpha unspent or overspent
    up, lo = switchback.crossing_probabilities([c.info_fraction for c in cps], z)
    assert up.sum() + lo.sum() == pytest.approx(alpha, abs=1e-6)


def test_checkpoints_keep_false_positive_rate_on_null_data():
    """Monte Carlo of the real procedure under the null: daily differences with no effect,
    looks only at the three pre-planned checkpoints."""
    n_days, alpha = 28, 0.10
    cps = switchback.plan_checkpoints(n_days, 3, alpha)
    rng = np.random.default_rng(20261004)
    n_sims = 200_000
    cum = np.cumsum(rng.standard_normal((n_sims, n_days), dtype=np.float32), axis=1)
    alive = np.ones(n_sims, dtype=bool)
    win = np.zeros(n_sims, dtype=bool)
    for c in cps:
        z = cum[:, c.day - 1] / np.sqrt(c.day)
        cross = alive & (np.abs(z) >= c.z_crit)
        win |= cross & (z < 0)  # treatment uses less runtime
        alive &= ~cross
    any_rate = 1 - alive.mean()
    se = np.sqrt(alpha * (1 - alpha) / n_sims)
    assert abs(any_rate - alpha) < 4 * se
    assert abs(win.mean() - alpha / 2) < 4 * np.sqrt(alpha / 2 / n_sims)
    # Peeking every day at the fixed-sample bar manufactures false wins (blueprint §4).
    z_daily = cum / np.sqrt(np.arange(1, n_days + 1))
    peek_rate = (np.abs(z_daily) >= stats.norm.isf(alpha / 2)).any(axis=1).mean()
    assert peek_rate > 2.5 * alpha


def test_sequential_inflation_is_small_and_grows_with_looks():
    assert switchback.sequential_inflation(0.10, 0.8, 1) == pytest.approx(1.0)
    two = switchback.sequential_inflation(0.10, 0.8, 2)
    three = switchback.sequential_inflation(0.10, 0.8, 3)
    assert 1.0 < two < three < 1.1


# ---------------------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("arms,block,n_days", [(["a", "b"], 2, 28), (["a", "b", "c"], 2, 30), (["a", "b"], 3, 20)])
def test_assign_days_balanced_blocks(arms, block, n_days):
    start = date(2026, 7, 1)
    sched = switchback.assign_days(arms, start, n_days, block, seed=42)
    assert [d for d, _ in sched] == [start + timedelta(days=i) for i in range(n_days)]
    blocks = [sched[i][1] for i in range(0, n_days, block)]
    for i in range(0, n_days, block):  # each block is one arm
        assert len({a for _, a in sched[i : i + block]}) == 1
    group = len(arms)
    full_groups = len(blocks) // group
    for g in range(full_groups):
        assert sorted(blocks[g * group : (g + 1) * group]) == sorted(arms)
    counts = {a: sum(1 for _, x in sched if x == a) for a in arms}
    assert max(counts.values()) - min(counts.values()) <= block


def test_assign_days_is_seeded():
    a = switchback.assign_days(["ctl", "trt"], date(2026, 7, 1), 28, 2, seed=7)
    b = switchback.assign_days(["ctl", "trt"], date(2026, 7, 1), 28, 2, seed=7)
    assert a == b
    others = {tuple(x for _, x in switchback.assign_days(["ctl", "trt"], date(2026, 7, 1), 28, 2, seed=s))
              for s in range(8)}
    assert len(others) > 1
    with pytest.raises(ValueError):
        switchback.assign_days(["a", "a"], date(2026, 7, 1), 10, 2, seed=1)


# ---------------------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------------------


def _body(**kw) -> ProposeExperimentBody:
    base = dict(name="Linked offset 1 vs 2", hypothesis="A 2°F offset cuts upstairs runtime",
                arms=[ArmIn(key="ctl", label="1°F", params={"linked_offset_f": 1.0}),
                      ArmIn(key="trt", label="2°F", params={"linked_offset_f": 2.0})],
                n_days=28, block_days=2, n_checkpoints=3, alpha=0.10)
    base.update(kw)
    return ProposeExperimentBody(**base)


@pytest.fixture
def guard_ok(monkeypatch):
    calls = []

    def fake(params, *, actor):
        calls.append((dict(params), actor))
        return [f"{k} is not a policy parameter" for k in params if k == "bogus"]

    monkeypatch.setattr("climate.control.guardrails.validate_policy_params", fake)
    return calls


def test_create_experiment_validates_and_stores_design(db, guard_ok):
    exp = switchback.create_experiment(db, _body(), "claude")
    assert exp.id and exp.status == "proposed" and exp.proposed_by == "claude"
    assert all(actor == "owner" for _, actor in guard_ok) and len(guard_ok) == 2
    d = exp.design
    assert d["n_days"] == 28 and d["block_days"] == 2 and d["alpha"] == 0.10 and isinstance(d["seed"], int)
    assert [c["day"] for c in d["checkpoints"]] == [9, 19, 28]
    assert sum(c["alpha_spent"] for c in d["checkpoints"]) == pytest.approx(0.10)

    bad = _body(arms=[ArmIn(key="ctl", label="a", params={}), ArmIn(key="trt", label="b", params={"bogus": 1})])
    with pytest.raises(ValueError, match="arm 'trt'"):
        switchback.create_experiment(db, bad, "owner")
    same = _body(arms=[ArmIn(key="ctl", label="a", params={"linked_offset_f": 1}),
                       ArmIn(key="trt", label="b", params={"linked_offset_f": 1})])
    with pytest.raises(ValueError, match="same settings"):
        switchback.create_experiment(db, same, "owner")
    with pytest.raises(ValueError, match="fewer than 2 days per arm"):
        switchback.create_experiment(db, _body(n_days=6, block_days=1, n_checkpoints=3), "owner")


def test_decide_and_active_arm_lifecycle(db, guard_ok):
    exp = switchback.create_experiment(db, _body(), "owner")
    with pytest.raises(ValueError):
        switchback.decide_experiment(db, exp.id, "stop", "not running yet")
    exp = switchback.decide_experiment(db, exp.id, "approve", "go ahead")
    now = utcnow()
    today = local_date(now, TZ)
    assert exp.status == "approved" and exp.start_date == today + timedelta(days=1)
    assert exp.end_date == exp.start_date + timedelta(days=27)
    days = db.scalars(select(ExperimentDay).where(ExperimentDay.experiment_id == exp.id)
                      .order_by(ExperimentDay.day)).all()
    assert len(days) == 28 and days[0].day == exp.start_date
    assert [x.arm for x in days] == [a for _, a in switchback.assign_days(["ctl", "trt"], exp.start_date, 28, 2,
                                                                          exp.design["seed"])]
    with pytest.raises(ValueError):
        switchback.decide_experiment(db, exp.id, "approve", "again")

    assert switchback.active_arm(db, now, TZ) is None  # starts tomorrow
    assert db.get(Experiment, exp.id).status == "approved"

    got = switchback.active_arm(db, now + timedelta(days=1), TZ)
    assert got is not None
    running, params = got
    assert running.id == exp.id and running.status == "running"
    want = {"ctl": {"linked_offset_f": 1.0}, "trt": {"linked_offset_f": 2.0}}[days[0].arm]
    assert params == want

    assert switchback.active_arm(db, now + timedelta(days=30), TZ) is None
    assert db.get(Experiment, exp.id).status == "completed"
    assert [e["event"] for e in db.get(Experiment, exp.id).result["log"]] == [
        "proposed", "approve", "running", "completed"]

    with pytest.raises(LookupError):
        switchback.decide_experiment(db, 999_999, "approve", "missing")


def test_second_approval_queues_after_the_first_and_stop_drops_days(db, guard_ok):
    first = switchback.decide_experiment(db, switchback.create_experiment(db, _body(), "owner").id, "approve", "ok")
    second = switchback.create_experiment(db, _body(n_days=14, n_checkpoints=2), "owner")
    second = switchback.decide_experiment(db, second.id, "approve", "ok")
    assert second.start_date == first.end_date + timedelta(days=1)

    stopped = switchback.decide_experiment(db, second.id, "stop", "changed my mind")
    assert stopped.status == "stopped" and stopped.start_date is None
    assert db.scalars(select(ExperimentDay).where(ExperimentDay.experiment_id == second.id)).all() == []

    rejected = switchback.create_experiment(db, _body(), "claude")
    assert switchback.decide_experiment(db, rejected.id, "reject", "not now").status == "rejected"


# ---------------------------------------------------------------------------------------
# filling days
# ---------------------------------------------------------------------------------------


def _insert_experiment(db, start: date, n_days: int, statuses: str = "running", arms=("ctl", "trt"),
                       checkpoints: int = 3, block_days: int = 2) -> Experiment:
    cps = switchback.plan_checkpoints(n_days, checkpoints, 0.10)
    exp = Experiment(name="test", hypothesis="h", arms=[{"key": a, "label": a.upper(), "params": {}} for a in arms],
                     design={"n_days": n_days, "block_days": block_days, "seed": 1, "alpha": 0.10,
                             "checkpoints": [c.model_dump() for c in cps]},
                     status=statuses, proposed_by="owner", start_date=start,
                     end_date=start + timedelta(days=n_days - 1))
    db.add(exp)
    db.flush()
    for d, arm in switchback.assign_days(list(arms), start, n_days, block_days, 1):
        db.add(ExperimentDay(experiment_id=exp.id, day=d, arm=arm))
    db.flush()
    return exp


def test_update_days_fills_house_residuals(db, monkeypatch):
    today = local_date(utcnow(), TZ)
    start = today - timedelta(days=6)
    exp = _insert_experiment(db, start, 10)
    sched = dict(switchback.assign_days(["ctl", "trt"], start, 10, 2, 1))
    thin_day, no_fit_day = start + timedelta(days=1), start + timedelta(days=2)

    def fake_rows(session, a, b, tz, unit_keys=None):
        out = []
        d = a
        while d <= b:
            for u in ("main", "up", "bed"):
                cool = 9000.0 if sched.get(d) == "trt" else 10000.0
                slots = 100 if (d == thin_day and u == "up") else 288
                out.append(SimpleNamespace(day=d, unit_key=u, cool_s=cool, heat_s=0.0, aux_s=0.0, slots=slots,
                                           mode="cool", outdoor_mean_f=85.0))
            d += timedelta(days=1)
        return out

    fits = {(u, "cool"): SimpleNamespace(unit_key=u) for u in ("main", "up", "bed")}

    def fake_expected(fit, row):
        if row.day == no_fit_day and fit.unit_key == "bed":
            raise AssertionError("bed has no fit that day")
        return 10000.0

    monkeypatch.setattr("climate.analytics.daily.daily_rows", fake_rows)
    monkeypatch.setattr("climate.analytics.baseline.expected_seconds", fake_expected)

    def fits_for(session):
        return fits

    monkeypatch.setattr("climate.analytics.baseline.active_fits", fits_for)
    # make the bed fit disappear on one day by filtering inside daily rows' consumer
    real_house_day = analysis._house_day

    def house_day(d, rows, units, weights, f, tz):
        if d == no_fit_day:
            f = {k: v for k, v in f.items() if k[0] != "bed"}
        return real_house_day(d, rows, units, weights, f, tz)

    monkeypatch.setattr(analysis, "_house_day", house_day)
    analysis.update_days(db, utcnow())

    rows = {r.day: r for r in db.scalars(select(ExperimentDay).where(ExperimentDay.experiment_id == exp.id))}
    for d, r in rows.items():
        if d >= today:
            assert r.actual_s is None and r.residual_s is None  # not finished yet
            continue
        if d == thin_day:
            assert not r.included and "90%" in r.note
        elif d == no_fit_day:
            assert not r.included and "no active cool baseline for bed" in r.note and r.residual_s is None
        else:
            assert r.included and r.note is None
            assert r.expected_s == pytest.approx(30000.0)
            want = -3000.0 if r.arm == "trt" else 0.0
            assert r.residual_s == pytest.approx(want)


# ---------------------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------------------


def _fill(db, exp: Experiment, effect_s: float, noise_s: float, seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    for r in db.scalars(select(ExperimentDay).where(ExperimentDay.experiment_id == exp.id)):
        r.expected_s = 20000.0
        r.residual_s = float(rng.normal(effect_s if r.arm == "trt" else 0.0, noise_s))
        r.actual_s = r.expected_s + r.residual_s
        r.included = True
    db.flush()


@pytest.fixture
def no_baselines(monkeypatch):
    monkeypatch.setattr("climate.analytics.baseline.active_fits", lambda session: {})


def test_analyze_not_started(db, no_baselines, guard_ok):
    exp = switchback.create_experiment(db, _body(), "owner")
    assert analysis.analyze(db, exp).decision == "not_started"
    exp = switchback.decide_experiment(db, exp.id, "approve", "ok")
    a = analysis.analyze(db, exp)
    assert a.decision == "not_started" and a.effect_pct is None and "starts" in a.note


def test_analyze_continue_between_looks(db, no_baselines):
    today = local_date(utcnow(), TZ)
    exp = _insert_experiment(db, today - timedelta(days=6), 28)  # 6 finished days; first look at day 9
    _fill(db, exp, -500.0, 800.0)
    a = analysis.analyze(db, exp)
    assert a.decision == "continue" and a.checkpoint_reached is None
    assert a.days_observed == 6 and "Informational" in a.note and "day 9" in a.note
    assert a.ci_low_pct < a.effect_pct < a.ci_high_pct


def test_analyze_stop_win_at_first_checkpoint(db, no_baselines):
    today = local_date(utcnow(), TZ)
    exp = _insert_experiment(db, today - timedelta(days=11), 28)  # past checkpoint 1 (day 9)
    _fill(db, exp, -3000.0, 300.0)  # treatment ~15% less runtime
    a = analysis.analyze(db, exp)
    assert a.decision == "stop_win" and a.checkpoint_reached == 1
    assert a.effect_pct == pytest.approx(-15.0, abs=2.0)
    assert a.ci_high_pct < 0
    # the checkpoint uses only its own 9 days, not the 11 observed
    assert "vs" in a.note and a.days_observed == 11


def test_analyze_final_look_futile_and_inconclusive(db, no_baselines):
    today = local_date(utcnow(), TZ)
    exp = _insert_experiment(db, today - timedelta(days=28), 28, statuses="completed")
    _fill(db, exp, +400.0, 300.0, seed=1)  # treatment worse
    a = analysis.analyze(db, exp)
    assert a.decision == "stop_futile" and a.checkpoint_reached == 3 and a.effect_pct > 0

    exp2 = _insert_experiment(db, today - timedelta(days=28), 28, statuses="completed")
    _fill(db, exp2, -150.0, 1500.0, seed=3)  # leans better, far too noisy to call
    b = analysis.analyze(db, exp2)
    assert b.checkpoint_reached == 3
    assert b.decision in ("inconclusive", "stop_futile")
    if b.effect_pct < 0:
        assert b.decision == "inconclusive" and b.ci_low_pct < 0 < b.ci_high_pct


def test_analyze_interval_widens_with_autocorrelated_baselines(db, monkeypatch):
    today = local_date(utcnow(), TZ)
    exp = _insert_experiment(db, today - timedelta(days=6), 28, block_days=3)
    _fill(db, exp, -500.0, 800.0)
    monkeypatch.setattr("climate.analytics.baseline.active_fits", lambda s: {})
    plain = analysis.analyze(db, exp)
    fits = {(u, "cool"): SimpleNamespace(cvrmse=0.15, resid_std_s=1500.0, resid_lag1=0.5) for u in ("main", "up", "bed")}
    monkeypatch.setattr("climate.analytics.baseline.active_fits", lambda s: fits)
    wide = analysis.analyze(db, exp)
    assert wide.effect_pct == pytest.approx(plain.effect_pct)
    assert (wide.ci_high_pct - wide.ci_low_pct) > (plain.ci_high_pct - plain.ci_low_pct)
    assert "lag-1" in wide.note


# ---------------------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------------------


def test_power_monotonic_and_honest(db, monkeypatch):
    monkeypatch.setattr("climate.analytics.baseline.active_fits", lambda s: {})
    none = analysis.power(db, 10.0)
    assert none.resid_cv is None and none.days_per_arm is None and "baselines" in none.note

    fits = {(u, "cool"): SimpleNamespace(cvrmse=0.15, resid_std_s=1500.0, resid_lag1=0.2)
            for u in ("main", "up", "bed")}
    monkeypatch.setattr("climate.analytics.baseline.active_fits", lambda s: fits)
    out = {e: analysis.power(db, e) for e in (5.0, 10.0, 15.0, 20.0)}
    days = [out[e].days_per_arm for e in (5.0, 10.0, 15.0, 20.0)]
    assert all(a > b for a, b in zip(days, days[1:]))
    assert out[10.0].resid_cv == pytest.approx(0.15)
    assert out[10.0].total_days == 2 * out[10.0].days_per_arm
    assert 7 <= out[15.0].days_per_arm <= 49  # 10-15% within weeks
    assert out[5.0].days_per_arm >= 50  # 5% takes months
    assert "simulator" in out[5.0].note
    # stricter alpha or more power needs more days
    assert analysis.power(db, 10.0, alpha=0.05).days_per_arm > out[10.0].days_per_arm
    assert analysis.power(db, 10.0, power_=0.9).days_per_arm > out[10.0].days_per_arm
    with pytest.raises(ValueError):
        analysis.power(db, 0.0)

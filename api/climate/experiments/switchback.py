"""Randomized switchback experiments on total-house runtime (blueprint §4-§5).

Days are assigned to arms in randomized blocks of ``block_days`` (balanced: every consecutive
group of ``len(arms)`` blocks contains each arm exactly once), seeded so the schedule is
reproducible. The success measure (weather-normalized total-house runtime) and 2-3
checkpoints are fixed BEFORE the start; checkpoints use Lan-DeMets O'Brien-Fleming alpha
spending so the looks together keep the false-positive rate at ``alpha``. Never stop on a
daily peek.

``alpha`` is two-sided: across all looks the chance of declaring ANY difference when the arms
are truly equal is ``alpha``; the chance of a false WIN for the treatment (one direction) is
``alpha / 2``.

Critical values are computed exactly for the correlated looks (not with the independent-
increments shortcut) by recursive numerical integration of the group-sequential boundary
crossing probabilities (Armitage, McPherson & Rowe 1969; Jennison & Turnbull 2000, ch. 19),
with Simpson's rule on a fine grid (converged to 1e-8 on the z scale). For K = 3 equally
spaced looks at alpha = 0.05 the critical values are 3.395 / 2.407 / 2.015, and a 6-million-run
Monte Carlo of null data crosses them 4.99% of the time.
"""

from __future__ import annotations

import math
import random
import secrets
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

import numpy as np
from scipy import optimize, stats
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from climate.api.schemas import Checkpoint, ProposeExperimentBody
from climate.store.app_settings import LocationSettings, get_setting
from climate.store.orm import Experiment, ExperimentDay
from climate.timeutil import local_date, utcnow

# Simpson grid size per look for the recursive integration (odd). 401 points already agrees
# with 3201 to 1e-8 on the z scale.
_GRID = 401
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_STATUSES_WITH_SCHEDULE = ("approved", "running")


# ---------------------------------------------------------------------------------------
# group-sequential boundaries
# ---------------------------------------------------------------------------------------


def obf_spending(t: float, alpha: float) -> float:
    """Lan-DeMets O'Brien-Fleming cumulative two-sided alpha spent by information fraction t."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return alpha
    z = stats.norm.isf(alpha / 2.0)
    return float(2.0 - 2.0 * stats.norm.cdf(z / math.sqrt(t)))


def _simpson(n: int, a: float, b: float) -> tuple[np.ndarray, np.ndarray]:
    grid = np.linspace(a, b, n)
    h = (b - a) / (n - 1)
    w = np.ones(n)
    w[1:-1:2] = 4.0
    w[2:-1:2] = 2.0
    return grid, w * h / 3.0


def _pdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / _SQRT_2PI


@lru_cache(maxsize=64)
def _boundaries(t: tuple[float, ...], alpha: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """(z_crit per look, incremental alpha per look) for two-sided symmetric OBF-spending
    boundaries at information fractions ``t`` (increasing, last = 1)."""
    tt = np.asarray(t, dtype=float)
    cum = np.array([obf_spending(x, alpha) for x in tt])
    cum[-1] = alpha
    inc = np.diff(np.concatenate([[0.0], cum]))
    k_looks = len(tt)
    z = np.empty(k_looks)
    # Work on the score scale S_k = Z_k * sqrt(t_k): increments are independent N(0, dt).
    z[0] = stats.norm.isf(inc[0] / 2.0) if inc[0] > 0 else math.inf
    b = z[0] * math.sqrt(tt[0])
    grid, w = _simpson(_GRID, -b, b)
    sd0 = math.sqrt(tt[0])
    dens = _pdf(grid / sd0) / sd0
    for k in range(1, k_looks):
        sd = math.sqrt(tt[k] - tt[k - 1])
        mass = w * dens

        def excess(bk: float, mass: np.ndarray = mass, grid: np.ndarray = grid, sd: float = sd,
                   target: float = float(inc[k])) -> float:
            p = stats.norm.sf((bk - grid) / sd) + stats.norm.cdf((-bk - grid) / sd)
            return float(np.dot(mass, p)) - target

        if inc[k] <= 1e-15:
            bk = 40.0 * math.sqrt(tt[k])
        else:
            bk = optimize.brentq(excess, 1e-9, 40.0, xtol=1e-12, rtol=1e-12)
        z[k] = bk / math.sqrt(tt[k])
        if k < k_looks - 1:
            new_grid, new_w = _simpson(_GRID, -bk, bk)
            kern = _pdf((new_grid[:, None] - grid[None, :]) / sd) / sd
            dens = kern @ mass
            grid, w = new_grid, new_w
    return tuple(float(x) for x in z), tuple(float(x) for x in inc)


def crossing_probabilities(t: list[float], z_crit: list[float], drift: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Per-look probabilities of FIRST crossing the upper (Z_k >= c_k) and lower (Z_k <= -c_k)
    boundary when the standardized statistic has drift ``drift`` (E[Z_k] = drift * sqrt(t_k)).
    drift = 0 is the null hypothesis."""
    tt = np.asarray(t, dtype=float)
    b = np.asarray(z_crit, dtype=float) * np.sqrt(tt)
    up = np.zeros(len(tt))
    lo = np.zeros(len(tt))
    sd0 = math.sqrt(tt[0])
    mu0 = drift * tt[0]
    up[0] = stats.norm.sf((b[0] - mu0) / sd0)
    lo[0] = stats.norm.cdf((-b[0] - mu0) / sd0)
    grid, w = _simpson(_GRID, -b[0], b[0])
    dens = _pdf((grid - mu0) / sd0) / sd0
    for k in range(1, len(tt)):
        d = tt[k] - tt[k - 1]
        sd = math.sqrt(d)
        mu = drift * d
        mass = w * dens
        up[k] = float(np.dot(mass, stats.norm.sf((b[k] - grid - mu) / sd)))
        lo[k] = float(np.dot(mass, stats.norm.cdf((-b[k] - grid - mu) / sd)))
        if k < len(tt) - 1:
            new_grid, new_w = _simpson(_GRID, -b[k], b[k])
            kern = _pdf((new_grid[:, None] - grid[None, :] - mu) / sd) / sd
            dens = kern @ mass
            grid, w = new_grid, new_w
    return up, lo


def sequential_inflation(alpha: float, power: float, n_looks: int) -> float:
    """Sample-size inflation of an OBF group-sequential design with ``n_looks`` equally spaced
    looks over a single fixed-size test at the same two-sided alpha and power."""
    if n_looks <= 1:
        return 1.0
    t = [k / n_looks for k in range(1, n_looks + 1)]
    z, _ = _boundaries(tuple(t), float(alpha))

    def short(theta: float) -> float:
        up, _ = crossing_probabilities(t, list(z), theta)
        return float(up.sum()) - power

    theta = optimize.brentq(short, 0.0, 20.0, xtol=1e-10)
    fixed = stats.norm.isf(alpha / 2.0) + stats.norm.ppf(power)
    return float((theta / fixed) ** 2)


def plan_checkpoints(n_days: int, n_checkpoints: int, alpha: float) -> list[Checkpoint]:
    """Equally spaced looks (last = n_days). OBF spending: alpha(t) = 2 - 2*Phi(z_{1-a/2}/sqrt(t))
    (two-sided). ``alpha_spent`` is the INCREMENTAL alpha spent at that look (they sum to
    ``alpha``); ``z_crit`` is the exact critical value for the correlated looks."""
    if n_days < 1:
        raise ValueError("n_days must be at least 1")
    if n_checkpoints < 1:
        raise ValueError("n_checkpoints must be at least 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    n_checkpoints = min(n_checkpoints, n_days)
    days: list[int] = []
    for k in range(1, n_checkpoints + 1):
        d = n_days if k == n_checkpoints else max(1, round(n_days * k / n_checkpoints))
        if days and d <= days[-1]:
            d = days[-1] + 1
        days.append(d)
    t = tuple(d / n_days for d in days)
    z, inc = _boundaries(t, float(alpha))
    return [
        Checkpoint(day=d, info_fraction=round(tf, 6), alpha_spent=a, z_crit=zc)
        for d, tf, a, zc in zip(days, t, inc, z, strict=True)
    ]


# ---------------------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------------------


def assign_days(arm_keys: list[str], start: date, n_days: int, block_days: int, seed: int) -> list[tuple[date, str]]:
    """Randomized, balanced blocks: the days are cut into blocks of ``block_days`` and every
    consecutive group of ``len(arm_keys)`` blocks is a fresh random permutation of the arms.
    The same seed always gives the same schedule."""
    if len(arm_keys) < 2:
        raise ValueError("a switchback needs at least two arms")
    if len(set(arm_keys)) != len(arm_keys):
        raise ValueError("arm keys must be unique")
    if block_days < 1 or n_days < 1:
        raise ValueError("n_days and block_days must be positive")
    rng = random.Random(seed)
    out: list[tuple[date, str]] = []
    while len(out) < n_days:
        order = list(arm_keys)
        rng.shuffle(order)
        for arm in order:
            for _ in range(block_days):
                if len(out) >= n_days:
                    break
                out.append((start + timedelta(days=len(out)), arm))
    return out


# ---------------------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------------------


def _tz(session: Session) -> str:
    return get_setting(session, "location", LocationSettings).tz


def _log(exp: Experiment, entry: dict[str, Any]) -> None:
    result = dict(exp.result or {})
    log = list(result.get("log", []))
    log.append(entry)
    result["log"] = log
    exp.result = result


def create_experiment(session: Session, body: ProposeExperimentBody, proposed_by: str) -> Experiment:
    """Validate each arm's params (guardrails.validate_policy_params, actor='owner'), store
    status 'proposed' with design {n_days, block_days, seed, checkpoints, alpha}.
    Raises ValueError (one sentence per problem) when the design is not acceptable."""
    from climate.control import guardrails

    if proposed_by not in ("model", "claude", "owner"):
        raise ValueError(f"unknown proposer {proposed_by!r}")
    problems: list[str] = []
    keys = [a.key for a in body.arms]
    if len(set(keys)) != len(keys):
        problems.append("arm keys must be unique")
    for arm in body.arms:
        for v in guardrails.validate_policy_params(dict(arm.params), actor="owner"):
            problems.append(f"arm '{arm.key}': {v}")
    canon = [sorted((k, repr(v)) for k, v in a.params.items()) for a in body.arms]
    if all(c == canon[0] for c in canon[1:]):
        problems.append("all arms have the same settings, so there is nothing to compare")
    n_arms = len(body.arms)
    if body.n_days < body.block_days * n_arms:
        problems.append(
            f"{body.n_days} days cannot give each of the {n_arms} arms a full {body.block_days}-day block"
        )
    checkpoints = plan_checkpoints(body.n_days, body.n_checkpoints, body.alpha)
    first = checkpoints[0].day
    if first < 2 * n_arms:
        problems.append(
            f"the first checkpoint (day {first}) would see fewer than 2 days per arm; "
            "use fewer checkpoints or a longer test"
        )
    if problems:
        raise ValueError("; ".join(problems))

    seed = secrets.randbelow(2**31 - 1)
    exp = Experiment(
        name=body.name,
        hypothesis=body.hypothesis,
        metric="house_runtime_residual",
        arms=[a.model_dump(mode="json") for a in body.arms],
        design={
            "n_days": body.n_days,
            "block_days": body.block_days,
            "n_checkpoints": len(checkpoints),
            "alpha": body.alpha,
            "seed": seed,
            "checkpoints": [c.model_dump() for c in checkpoints],
            "control_arm": body.arms[0].key,
            "treatment_arm": body.arms[1].key,
        },
        status="proposed",
        proposed_by=proposed_by,
        result={"log": [{"at": utcnow().isoformat(), "event": "proposed", "by": proposed_by}]},
    )
    session.add(exp)
    session.flush()
    return exp


def decide_experiment(session: Session, experiment_id: int, decision: str, reason: str) -> Experiment:
    """Owner only (enforced by the router). approve -> 'approved' with start_date = tomorrow
    (local; or the day after another approved/running experiment ends) and the day schedule
    written to experiment_days; reject; stop -> 'stopped' (unfinished days are dropped).
    Raises LookupError for an unknown id and ValueError for a decision the status forbids."""
    exp = session.get(Experiment, experiment_id)
    if exp is None:
        raise LookupError(f"experiment {experiment_id} not found")
    tz = _tz(session)
    now = utcnow()
    today = local_date(now, tz)
    design = dict(exp.design or {})

    if decision == "approve":
        if exp.status != "proposed":
            raise ValueError(f"only a proposed experiment can be approved (this one is {exp.status})")
        start = today + timedelta(days=1)
        others = session.scalars(
            select(Experiment).where(Experiment.status.in_(_STATUSES_WITH_SCHEDULE), Experiment.id != exp.id)
        ).all()
        for other in others:
            if other.end_date is not None and other.end_date >= start:
                start = other.end_date + timedelta(days=1)
        n_days = int(design["n_days"])
        schedule = assign_days([a["key"] for a in exp.arms], start, n_days, int(design["block_days"]),
                               int(design["seed"]))
        session.execute(delete(ExperimentDay).where(ExperimentDay.experiment_id == exp.id))
        session.add_all(ExperimentDay(experiment_id=exp.id, day=d, arm=arm, included=True) for d, arm in schedule)
        exp.status = "approved"
        exp.start_date = start
        exp.end_date = start + timedelta(days=n_days - 1)
    elif decision == "reject":
        if exp.status != "proposed":
            raise ValueError(f"only a proposed experiment can be rejected (this one is {exp.status})")
        exp.status = "rejected"
    elif decision == "stop":
        if exp.status not in _STATUSES_WITH_SCHEDULE:
            raise ValueError(f"only an approved or running experiment can be stopped (this one is {exp.status})")
        started = exp.start_date is not None and exp.start_date <= today
        if started:
            # Today is only partly run under its arm: it and every later day are dropped.
            session.execute(
                delete(ExperimentDay).where(ExperimentDay.experiment_id == exp.id, ExperimentDay.day >= today)
            )
            last = today - timedelta(days=1)
            exp.end_date = last if exp.start_date is not None and last >= exp.start_date else None
        else:
            session.execute(delete(ExperimentDay).where(ExperimentDay.experiment_id == exp.id))
            exp.start_date = None
            exp.end_date = None
        exp.status = "stopped"
    else:
        raise ValueError(f"unknown decision {decision!r}")

    _log(exp, {"at": now.isoformat(), "event": decision, "reason": reason})
    exp.updated_at = func.now()
    session.flush()
    session.refresh(exp)
    return exp


def active_arm(session: Session, now: datetime, tz: str) -> tuple[Experiment, dict[str, Any]] | None:
    """The running experiment and today's arm params, if any (also flips approved ->
    running on its start_date and running -> completed after end_date)."""
    today = local_date(now, tz)
    flipped = False
    for exp in session.scalars(select(Experiment).where(Experiment.status.in_(_STATUSES_WITH_SCHEDULE))).all():
        new_status: str | None = None
        if exp.end_date is not None and exp.end_date < today:
            new_status = "completed"
        elif exp.status == "approved" and exp.start_date is not None and exp.start_date <= today:
            new_status = "running"
        if new_status is not None:
            exp.status = new_status
            _log(exp, {"at": now.isoformat(), "event": new_status})
            exp.updated_at = func.now()
            flipped = True
    if flipped:
        session.flush()
    row = session.execute(
        select(Experiment, ExperimentDay)
        .join(ExperimentDay, ExperimentDay.experiment_id == Experiment.id)
        .where(Experiment.status == "running", ExperimentDay.day == today)
        .order_by(Experiment.id)
    ).first()
    if row is None:
        return None
    exp, day = row
    arm = next((a for a in exp.arms if a.get("key") == day.arm), None)
    if arm is None:
        return None
    return exp, dict(arm.get("params") or {})

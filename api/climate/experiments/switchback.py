"""Randomized switchback experiments on total-house runtime (blueprint §4-§5).

Days are assigned to arms in randomized blocks of ``block_days`` (balanced within each pair
of blocks), seeded so the schedule is reproducible. The success measure (weather-normalized
total-house runtime) and 2-3 checkpoints are fixed BEFORE the start; checkpoints use
Lan-DeMets O'Brien-Fleming alpha spending so the looks together keep the false-win rate at
``alpha``. Never stop on a daily peek.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from climate.api.schemas import Checkpoint, ProposeExperimentBody
from climate.store.orm import Experiment


def plan_checkpoints(n_days: int, n_checkpoints: int, alpha: float) -> list[Checkpoint]:
    """Equally spaced looks (last = n_days). OBF spending: alpha(t) = 2 - 2*Phi(z_{a/2}/sqrt(t))
    (two-sided). z_crit per look from the incremental spend (independent-increments approx)."""
    raise NotImplementedError


def assign_days(arm_keys: list[str], start: date, n_days: int, block_days: int, seed: int) -> list[tuple[date, str]]:
    raise NotImplementedError


def create_experiment(session: Session, body: ProposeExperimentBody, proposed_by: str) -> Experiment:
    """Validate each arm's params (guardrails.validate_policy_params, actor='owner'), store
    status 'proposed' with design {n_days, block_days, seed, checkpoints, alpha}."""
    raise NotImplementedError


def decide_experiment(session: Session, experiment_id: int, decision: str, reason: str) -> Experiment:
    """Owner only (enforced by the router). approve -> 'approved' with start_date = tomorrow
    (local) and the day schedule written to experiment_days; reject; stop -> 'stopped'."""
    raise NotImplementedError


def active_arm(session: Session, now: datetime, tz: str) -> tuple[Experiment, dict[str, Any]] | None:
    """The running experiment and today's arm params, if any (also flips approved ->
    running on its start_date and running -> completed after end_date)."""
    raise NotImplementedError

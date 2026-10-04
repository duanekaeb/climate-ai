"""Experiment analysis and power, on weather-normalized daily total-house runtime."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from climate.api.schemas import ExperimentAnalysis, PowerOut
from climate.store.orm import Experiment


def update_days(session: Session, now: datetime) -> None:
    """Fill actual_s / expected_s / residual_s on experiment_days for finished local days."""
    raise NotImplementedError


def analyze(session: Session, experiment: Experiment) -> ExperimentAnalysis:
    """Effect = mean residual(arm B) - mean residual(arm A), as % of mean expected runtime;
    Welch interval at the current checkpoint's alpha (or 90% when not at a checkpoint and
    say it's informational). Decision only at a pre-planned checkpoint."""
    raise NotImplementedError


def power(session: Session, effect_pct: float, alpha: float = 0.10, power_: float = 0.8) -> PowerOut:
    """Days per arm needed from the active baselines' residual CV (two-sample normal approx,
    inflated for lag-1 autocorrelation). Without baselines: resid_cv None, days None."""
    raise NotImplementedError

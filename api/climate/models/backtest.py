"""Backtests and simulations for the change gates and the agent's tools."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from climate.api.schemas import BacktestOut, SimulateOut


def backtest(session: Session, params: dict, days: int = 28) -> BacktestOut:
    """Replay the last ``days`` with the current policy and with the candidate params
    (partial PolicyParams over the active ones). With an active RC fit: simulate both and
    compare total runtime and comfort-violation minutes; ci90 from the fit's residual error.
    Without one: model='rule_of_thumb' (runtime sensitivity per °F of setpoint from the
    baselines' slope), beats_model_uncertainty False unless the effect exceeds 2x the
    baseline CV."""
    raise NotImplementedError


def simulate(session: Session, params: dict, day: date | None = None) -> SimulateOut:
    raise NotImplementedError

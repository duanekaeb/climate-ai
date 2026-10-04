"""Separate weather from strategy: avoided-runtime savings and the weekly waterfall."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from climate.api.schemas import Savings, Waterfall


def savings(session: Session, start: date, end: date) -> Savings:
    """Savings = expected - actual (IPMVP avoided energy) on total-house stage-1 runtime,
    weighted by units.power_weight. 90% interval from the baseline residual std with the
    lag-1 autocorrelation adjustment (ASHRAE G14 fractional savings uncertainty). If any
    involved baseline fails its checks, baseline_ok = False and savings fields are None."""
    raise NotImplementedError


def waterfall(session: Session, week_start: date) -> Waterfall:
    """Last week total -> weather effect (expected this week - expected last week) ->
    strategy & other (the rest) -> this week total, with a 90% interval on the strategy bar."""
    raise NotImplementedError

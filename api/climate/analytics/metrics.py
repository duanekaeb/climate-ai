"""Comfort, duty, maxed-out minutes and drift."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from climate.api.schemas import ComfortRow, DriftReport


def unit_today(session: Session, now: datetime, tz: str) -> dict[str, dict[str, float | None]]:
    """unit_key -> {'today_runtime_min', 'duty_last_hour_pct', 'maxed_minutes_today'}."""
    raise NotImplementedError


def comfort(session: Session, days: int = 7) -> list[ComfortRow]:
    """Per sensored room: minutes occupied/asleep (room_states) and the share of those
    minutes inside the unit's comfort band for the period (readings_5m temp vs band)."""
    raise NotImplementedError


def drift(session: Session, recent_days: int = 7) -> DriftReport:
    """Recent mean residual of each active baseline vs its training residual spread; z > 2
    for the recent window -> drifting (triggers a Claude investigation via the worker)."""
    raise NotImplementedError

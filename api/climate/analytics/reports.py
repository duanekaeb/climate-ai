"""Deterministic daily digest (the system writes it; Claude's nightly run adds judgment)."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session


def build_daily_report(session: Session, day: date) -> int:
    """Write a reports row (kind='daily', author='system') for a local day: runtime per unit
    vs expected, maxed-out minutes, comfort misses, controller actions, alerts. Markdown body,
    numbers also in data. Idempotent per day (replace the system daily report for that day).
    Returns the report id."""
    raise NotImplementedError

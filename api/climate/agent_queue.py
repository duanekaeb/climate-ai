"""The agent_runs queue shared by the API (enqueue, claim, finish) and the worker (schedule,
anomaly triggers). The agent service talks to it only through the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from climate.store.orm import AgentRun


class TriggerLimitReached(RuntimeError):
    pass


def enqueue(session: Session, kind: str, requested_by: str, prompt: str = "", trigger: dict[str, Any] | None = None) -> AgentRun:
    """Insert a queued run. 'triggered' runs: at most AgentSettings.max_triggered_per_day per
    local day, and coalesced with an already queued triggered run (append the trigger)."""
    raise NotImplementedError


def claim_next(session: Session, now: datetime) -> AgentRun | None:
    """Oldest queued (or deferred with not_before <= now) run -> running (SELECT ... FOR UPDATE
    SKIP LOCKED). Chat runs first."""
    raise NotImplementedError


def finish(session: Session, run_id: int, **fields: Any) -> AgentRun:
    raise NotImplementedError


def schedule_due(session: Session, now: datetime) -> list[int]:
    """Enqueue the nightly / weekly runs when their local time has passed today and none
    exists yet for this period. Returns new ids."""
    raise NotImplementedError

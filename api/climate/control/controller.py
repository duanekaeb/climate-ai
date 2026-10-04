"""The controller: every tick, plan -> guardrails -> suggest or act -> read back -> log.

Modes (app_settings control.mode): 'off' does nothing; 'suggest' logs control_actions with
status 'suggested' and never writes; 'act' writes timed holds (holdHours, 1-2 h, renewed
while healthy) through the active source and records the read-back. Every write is logged
to control_actions with channel, reason, before/after and read-back (CLAUDE.md).
Claude is never in this path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from climate.api.schemas import PlanRow
from climate.sources.base import ThermostatSource


def current_plan(session: Session, now: datetime | None = None) -> list[PlanRow]:
    """Plan + guard for every unit without writing anything (GET /api/control/plan)."""
    raise NotImplementedError


async def tick(source: ThermostatSource | None, now: datetime | None = None) -> list[int]:
    """One controller pass. Persists room states, plans, guards, then per mode logs or
    writes. Skips a unit whose current hold already matches the target (renews a hold of
    ours that ends within 20 minutes). Detects manual changes (a hold not set_by_us) and
    starts the manual back-off. Returns the control_action ids created. Never raises on a
    single unit's failure; logs it and continues."""
    raise NotImplementedError


async def execute_queued(source: ThermostatSource | None, now: datetime | None = None) -> list[int]:
    """Execute owner-queued actions (POST /api/control/hold|resume insert status='queued',
    channel matching the source). HomeKit-channel rows are left for the homekit service."""
    raise NotImplementedError

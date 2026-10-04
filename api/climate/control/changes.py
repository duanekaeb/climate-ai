"""The change gates (blueprint §4): backtest -> shadow -> sign-off -> trial -> active.

proposed_by 'model' -> Claude may approve/hold if every changed key is inside
CLAUDE_SIGNOFF_RANGES; otherwise, and for anything Claude proposes, the owner decides.
The owner can approve anything that passes the hard-limit validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from climate.control.policy import PolicyParams
from climate.store.orm import Change

SHADOW_DAYS = 3
TRIAL_DAYS = 7


def active_policy(session: Session) -> tuple[PolicyParams, int | None]:
    """Params and id of the active policy_versions row (defaults, None if none)."""
    raise NotImplementedError


def propose_policy_change(
    session: Session, proposed_by: Literal["model", "claude", "owner"], title: str, rationale: str, params: dict,
) -> Change:
    """Validate (guardrails.validate_policy_params with actor), insert a 'policy' change in
    status 'backtest' (or 'rejected' with gates.validation errors), publish 'change'."""
    raise NotImplementedError


def decide(
    session: Session, change_id: int, actor: Literal["claude", "owner"],
    decision: Literal["approve", "hold", "reject"], reason: str,
) -> Change:
    """Apply a decision. Raises PermissionError when the actor may not decide this change,
    ValueError when the change is not awaiting a decision. Approve -> 'trial' (creates a
    policy_versions row status 'trial') ; owner may approve from 'backtest'/'shadow' too
    (skipping remaining gates is recorded in gates)."""
    raise NotImplementedError


def needs(change: Change) -> Literal["nothing", "claude", "owner"]:
    raise NotImplementedError


def advance(session: Session, now: datetime) -> list[int]:
    """Move changes through the gates: run the backtest (climate.models.backtest.backtest)
    for 'backtest' -> 'shadow' (or 'rejected' if it does not beat model uncertainty);
    after SHADOW_DAYS -> 'awaiting_signoff' (+ enqueue nothing; Claude sees it nightly);
    'trial' for TRIAL_DAYS with no comfort regressions -> 'active' (retire the old
    policy version). Returns ids that changed."""
    raise NotImplementedError

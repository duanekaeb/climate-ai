"""Alerts (table) and push notifications (ntfy)."""

from __future__ import annotations

from sqlalchemy.orm import Session


def raise_alert(session: Session, kind: str, level: str, title: str, body: str = "", dedupe_key: str | None = None) -> int | None:
    """Insert an alert unless an open one has the same dedupe_key; publish 'alert'; send a
    push for warn/error (best effort, never raises). Returns the id or None if deduped."""
    raise NotImplementedError


def resolve_alert(session: Session, dedupe_key: str) -> None:
    raise NotImplementedError


def push(title: str, body: str, priority: str = "default", tags: list[str] | None = None) -> bool:
    """POST to {ntfy_url}/{ntfy_topic}; returns False if disabled or failed."""
    raise NotImplementedError

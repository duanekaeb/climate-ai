"""Alerts (table) and push notifications (ntfy).

``raise_alert`` dedupes on ``dedupe_key`` through the partial unique index
``uq_alerts_open_dedupe`` (one OPEN alert per key); ``resolve_alert`` closes it, so the same
condition can alert again later. warn/error alerts are pushed through ntfy when
``CLIMATE_NTFY_URL`` is set. Pushing is best effort and never raises: a dead ntfy server must
not break the code path that noticed the problem.
"""

from __future__ import annotations

import base64
import logging

import httpx
from sqlalchemy import and_, func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.config import get_settings
from climate.events import publish
from climate.store.orm import Alert

log = logging.getLogger(__name__)

LEVELS = ("info", "warn", "error")
PUSH_TIMEOUT_S = 5.0
_PUSH_PRIORITY = {"warn": "default", "error": "high"}
_PUSH_TAG = {"warn": "warning", "error": "rotating_light"}

# Tests inject an ``httpx.MockTransport`` here; None = the real network.
_TRANSPORT: httpx.BaseTransport | None = None


def raise_alert(session: Session, kind: str, level: str, title: str, body: str = "", dedupe_key: str | None = None) -> int | None:
    """Insert an alert unless an open one has the same dedupe_key; publish 'alert'; send a
    push for warn/error (best effort, never raises). Returns the id or None if deduped."""
    if level not in LEVELS:
        raise ValueError(f"alert level must be one of {LEVELS}, not {level!r}")
    stmt = insert(Alert).values(level=level, kind=kind, title=title, body=body, dedupe_key=dedupe_key)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[Alert.dedupe_key],
            index_where=and_(Alert.resolved_at.is_(None), Alert.dedupe_key.is_not(None)),
        )
    alert_id = session.execute(stmt.returning(Alert.id)).scalar_one_or_none()
    if alert_id is None:
        return None
    publish(session, "alert", alert_id)
    if level in _PUSH_PRIORITY:
        tags = [_PUSH_TAG[level], kind.split(":", 1)[0]]
        if push(title, body, priority=_PUSH_PRIORITY[level], tags=tags):
            session.execute(update(Alert).where(Alert.id == alert_id).values(notified_at=func.now()))
    return alert_id


def resolve_alert(session: Session, dedupe_key: str) -> None:
    """Close the open alert with this dedupe_key (no-op when none is open)."""
    result = session.execute(
        update(Alert)
        .where(Alert.dedupe_key == dedupe_key, Alert.resolved_at.is_(None))
        .values(resolved_at=func.now())
        .returning(Alert.id)
    )
    ids = list(result.scalars())
    for alert_id in ids:
        publish(session, "alert", alert_id)


def _header(value: str) -> str:
    """HTTP headers are ASCII; ntfy decodes RFC 2047 encoded words for anything else."""
    value = " ".join(value.splitlines()).strip()
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def push(title: str, body: str, priority: str = "default", tags: list[str] | None = None) -> bool:
    """POST to {ntfy_url}/{ntfy_topic}; returns False if disabled or failed."""
    cfg = get_settings()
    if not cfg.ntfy_url or not cfg.ntfy_topic:
        return False
    if "ntfy.sh" in cfg.ntfy_url and len(cfg.ntfy_topic) < 16:
        log.warning("push disabled: a topic on the public ntfy.sh must be long and random (16+ chars)")
        return False
    url = f"{cfg.ntfy_url.rstrip('/')}/{cfg.ntfy_topic.strip('/')}"
    headers = {"Title": _header(title or "Climate AI"), "Priority": _header(priority)}
    if tags:
        headers["Tags"] = _header(",".join(t for t in tags if t))
    if cfg.public_url:
        headers["Click"] = _header(cfg.public_url)
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    try:
        with httpx.Client(timeout=PUSH_TIMEOUT_S, transport=_TRANSPORT) as client:
            resp = client.post(url, content=(body or title).encode("utf-8"), headers=headers)
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        log.warning("ntfy push failed: %s", type(exc).__name__)
        return False
    if resp.is_success:
        return True
    log.warning("ntfy push failed: HTTP %s", resp.status_code)
    return False

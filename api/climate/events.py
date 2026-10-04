"""Change notifications for the web app's live view.

Writers call ``publish(session, "status")`` inside their transaction; Postgres delivers the
NOTIFY on commit. The API's websocket hub (``climate.api.routers.ws``) LISTENs on
``climate_events`` and fans the tiny event out to browsers, which refetch what changed.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from climate.store.db import notify

CHANNEL = "climate_events"
EVENT_TYPES = {"status", "action", "agent_run", "alert", "homekit", "change", "report"}


def publish(session: Session, type_: str, id_: int | None = None) -> None:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    notify(session, CHANNEL, json.dumps({"type": type_, "id": id_}))

"""The cloud/simulator poller loop body (the worker schedules it).

ecobee: /thermostatSummary every poll_seconds (>= 180); /thermostat detail only for units
whose revision changed; runtimeReport hourly for the last 2 h and nightly for the last 2 days
(one request at a time). Simulator: advance, snapshot, runtime every sim_poll_seconds. Fetched
snapshots also update the utility (demand-response) events they list
(``climate.utility.events.ingest``).

Failure handling (``record_source_failure`` / ``record_source_success``, also used by the
worker when building a source fails):
- an ``EcobeeAuthError`` raises the 'ecobee_auth' error alert at once ("Sign in to ecobee
  again in Setup");
- 3 consecutive failures raise the 'source_down' alert, and for ecobee with HomeKit enabled
  open the cloud circuit breaker for 15 minutes (``SourceSettings.cloud_circuit_open_until``;
  the controller then queues HomeKit holds). Every further failure keeps it open;
- the first success resolves both alerts and closes the circuit.
Error text is redacted before it is logged or stored: ecobee's token endpoint carries the
refresh token in its query string, and exception messages often quote the URL.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from climate import notify
from climate.collector.ingest import ingest_runtime, ingest_snapshots, runtime_source_name
from climate.sources.base import RuntimeInterval, ThermostatSource, UnitSnapshot
from climate.store.app_settings import SourceSettings, beat, get_heartbeat, get_setting, put_setting
from climate.store.db import session_scope
from climate.store.orm import AppSetting
from climate.timeutil import floor_slot, utcnow

log = logging.getLogger(__name__)

FAILURES_FOR_ALERT = 3
CIRCUIT_OPEN_FOR = timedelta(minutes=15)

_QUERY = re.compile(r"\?[^\s'\"<>]*")
_SECRETISH = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:[a-z]+\s+)?|bearer\s+"
    r"|(?:refresh_|access_)?token[\"']?\s*[:=]\s*[\"']?|password[\"']?\s*[:=]\s*[\"']?|code=)[^\s,'\"&;}]+"
)


def redact(text: str) -> str:
    """Strip query strings and token-looking values from text bound for logs or the DB."""
    return _SECRETISH.sub(r"\1[redacted]", _QUERY.sub("?[redacted]", text))


def safe_error(exc: BaseException, limit: int = 300) -> str:
    msg = str(exc).strip()
    text = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return redact(text)[:limit]


def is_auth_error(exc: BaseException) -> bool:
    try:
        from climate.sources.ecobee import EcobeeAuthError
    except Exception:  # noqa: BLE001 - the adapter may fail to import (missing library)
        return type(exc).__name__ == "EcobeeAuthError"
    return isinstance(exc, EcobeeAuthError)


@dataclass
class SourceState:
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    auth_failed: bool = False
    needs_resolve: bool = True  # first success after start clears alerts a previous run left


_STATES: dict[str, SourceState] = {}


def source_state(kind: str) -> SourceState:
    return _STATES.setdefault(kind, SourceState())


def reset_states() -> None:
    _STATES.clear()


def merge_beat(session: Session, service: str, ok: bool = True, **detail: Any) -> None:
    """Heartbeat that keeps keys other writers put in ``detail`` (beat() replaces them)."""
    session.execute(select(AppSetting.key).where(AppSetting.key == f"heartbeat:{service}").with_for_update())
    hb = get_heartbeat(session, service)
    merged: dict[str, Any] = dict(hb.detail) if hb else {}
    for key, value in detail.items():
        merged[key] = value.isoformat() if isinstance(value, datetime) else value
    beat(session, service, ok=ok, **merged)


def _set_circuit(session: Session, until: datetime | None) -> None:
    raw = session.execute(
        select(AppSetting.value).where(AppSetting.key == "source").with_for_update()
    ).scalar_one_or_none()
    current = SourceSettings.model_validate(raw) if raw is not None else SourceSettings()
    if current.cloud_circuit_open_until == until:
        return
    put_setting(session, "source", current.model_copy(update={"cloud_circuit_open_until": until}), updated_by="worker")


def record_source_failure(kind: str, exc: BaseException, now: datetime) -> SourceState:
    """Count a failed source call; raise alerts / open the circuit as described above."""
    st = source_state(kind)
    st.consecutive_failures += 1
    st.last_failure_at = now
    st.last_error = safe_error(exc)
    st.auth_failed = is_auth_error(exc)
    st.needs_resolve = True
    log.warning("%s source failed (%d in a row): %s", kind, st.consecutive_failures, st.last_error)
    with session_scope() as s:
        if st.auth_failed:
            notify.raise_alert(
                s, "ecobee_auth", "error", "Sign in to ecobee again in Setup",
                "The ecobee cloud refused the saved sign-in. Live data and holds through the cloud are "
                "paused until you sign in again (Setup > ecobee).",
                dedupe_key="ecobee_auth",
            )
        if st.consecutive_failures >= FAILURES_FOR_ALERT:
            name = "ecobee cloud" if kind == "ecobee" else "simulator"
            notify.raise_alert(
                s, "source_down", "warn", f"The {name} is not responding",
                f"{st.consecutive_failures} polls in a row failed. Last error: {st.last_error}",
                dedupe_key="source_down",
            )
            if kind == "ecobee" and get_setting(s, "source", SourceSettings).homekit_enabled:
                _set_circuit(s, now + CIRCUIT_OPEN_FOR)
        merge_beat(s, "worker", source=kind, source_ok=False, source_consecutive_failures=st.consecutive_failures,
                   source_error=st.last_error, source_last_failure_at=now)
    return st


def record_source_success(kind: str, now: datetime) -> SourceState:
    st = source_state(kind)
    st.consecutive_failures = 0
    st.last_success_at = now
    st.auth_failed = False
    with session_scope() as s:
        if st.needs_resolve:
            notify.resolve_alert(s, "source_down")
            if kind == "ecobee":
                notify.resolve_alert(s, "ecobee_auth")
                _set_circuit(s, None)
            st.needs_resolve = False
        merge_beat(s, "worker", source=kind, source_ok=True, source_consecutive_failures=0, source_error=None,
                   source_last_success_at=now, last_poll_at=now)
    return st


def _ingest_snapshots_tx(snaps: list[UnitSnapshot], now: datetime | None = None) -> None:
    """Store live snapshots, then the utility events they list (``climate.utility.events.ingest``,
    live polls only: backfill's simulated history goes through ``ingest_snapshots`` alone). The
    events step runs in a savepoint: if it fails, it alone is rolled back and logged, and the
    poll still counts as a success."""
    with session_scope() as s:
        ingest_snapshots(s, snaps)
        try:
            with s.begin_nested():
                from climate.utility import events as utility_events

                utility_events.ingest(s, snaps, now or utcnow())
        except Exception as exc:  # noqa: BLE001 - never fails the poll
            log.warning("utility events ingest failed: %s", safe_error(exc))


def _ingest_runtime_tx(intervals: list[RuntimeInterval], source: str) -> int:
    with session_scope() as s:
        return ingest_runtime(s, intervals, source)


async def poll_once(source: ThermostatSource, now: datetime, last_revisions: dict[str, str]) -> dict[str, str]:
    """One summary/detail pass. Returns the new revisions map. Updates heartbeat 'worker'
    detail.source_*; raises nothing on transient errors (records an alert after 3 failures).

    Snapshots are fetched only for units whose revision changed (all units on the first call,
    when ``last_revisions`` is empty). On failure the old map is returned, so the next pass
    retries the same units."""
    kind = source.kind
    try:
        revisions = dict(await source.poll_revisions())
        if last_revisions:
            changed = sorted(k for k, rev in revisions.items() if last_revisions.get(k) != rev)
            snaps = list(await source.fetch_snapshots(changed)) if changed else []
        else:
            snaps = list(await source.fetch_snapshots(None))
        if snaps:
            await asyncio.to_thread(_ingest_snapshots_tx, snaps, now)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - transient by contract; counted and alerted
        try:
            await asyncio.to_thread(record_source_failure, kind, exc, now)
        except Exception as inner:  # noqa: BLE001 - the DB itself may be the problem
            log.warning("could not record the source failure: %s", safe_error(inner))
        return dict(last_revisions)
    try:
        await asyncio.to_thread(record_source_success, kind, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record the source success: %s", safe_error(exc))
    return revisions


async def pull_runtime(source: ThermostatSource, now: datetime, hours_back: int = 2) -> int:
    """Fetch the equipment record for the last ``hours_back`` hours and upsert it (a re-pull
    of the same slots overwrites them: ecobee fills recent slots in with up to ~1 h lag).
    Returns the runtime rows written. Errors propagate to the caller (the worker logs them)."""
    start = floor_slot(now - timedelta(hours=max(1, hours_back)))
    intervals = list(await source.fetch_runtime(start, now))
    if not intervals:
        return 0
    return await asyncio.to_thread(_ingest_runtime_tx, intervals, runtime_source_name(source.kind))

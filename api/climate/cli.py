"""Admin CLI: ``python -m climate.cli <command>``.

Commands: migrate | seed | backfill --days N | gen-key | set-password | doctor
(doctor checks DB, secrets key, source sign-in, HomeKit heartbeat, agent heartbeat).

``backfill`` runs in this process with the configured source, except when the source is
ecobee and the worker is running: only the worker may refresh ecobee tokens, so the request
is queued as a 'backfill' job for it instead.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets as pysecrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

WORKER_FRESH = timedelta(minutes=5)
HOMEKIT_FRESH = timedelta(minutes=3)
AGENT_FRESH = timedelta(minutes=15)


def _print(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")


# ---------------------------------------------------------------------------------------
# simple commands
# ---------------------------------------------------------------------------------------


def cmd_migrate(_: argparse.Namespace) -> int:
    from climate.store.migrate import migrate

    applied = migrate()
    _print(f"applied: {', '.join(applied)}" if applied else "database is up to date")
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    from climate.house import seed
    from climate.store.db import session_scope

    with session_scope() as s:
        seed(s)
    _print("seeded (existing rows and owner edits are kept)")
    return 0


def cmd_gen_key(_: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    _print(f"CLIMATE_SECRET_KEY={Fernet.generate_key().decode()}")
    _print(f"CLIMATE_SESSION_SECRET={pysecrets.token_urlsafe(48)}")
    return 0


def cmd_set_password(args: argparse.Namespace, prompt: Callable[[str], str] = getpass.getpass) -> int:
    from climate.api.auth import set_owner_password
    from climate.config import get_settings

    first = prompt("New owner password: ")
    if len(first) < 8 or len(first) > 200:
        _print("The password must be 8 to 200 characters.")
        return 1
    if prompt("Repeat it: ") != first:
        _print("The passwords do not match; nothing changed.")
        return 1
    set_owner_password(first)
    _print("Owner password saved.")
    if get_settings().owner_password:
        _print("Note: CLIMATE_OWNER_PASSWORD is set and takes priority over the saved password.")
    return 0


def _worker_fresh(now: datetime) -> bool:
    from climate.store.app_settings import get_heartbeat
    from climate.store.db import session_scope

    with session_scope() as s:
        hb = get_heartbeat(s, "worker")
    return hb is not None and now - hb.at <= WORKER_FRESH


def cmd_backfill(args: argparse.Namespace) -> int:
    from climate.store.app_settings import SourceSettings, get_setting
    from climate.store.db import session_scope
    from climate.store.orm import Job
    from climate.timeutil import utcnow

    days = int(args.days)
    if days < 1 or days > 3650:
        _print("--days must be between 1 and 3650")
        return 1
    now = utcnow()
    with session_scope() as s:
        kind = get_setting(s, "source", SourceSettings).kind
    if kind == "ecobee" and _worker_fresh(now):
        with session_scope() as s:
            job = Job(kind="backfill", status="queued", requested_by="cli", params={"days": days})
            s.add(job)
            s.flush()
            job_id = job.id
        _print(f"The worker is running and owns the ecobee sign-in; queued backfill job {job_id} for it.")
        return 0

    async def run() -> dict[str, Any]:
        from climate.collector.backfill import backfill
        from climate.sources import get_source

        source = get_source(kind)
        try:
            return await backfill(source, now - timedelta(days=days), now)
        finally:
            await source.close()

    result = asyncio.run(run())
    _print(json.dumps(result, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str  # 'ok' | 'fail' | 'skip'
    detail: str


def _age(now: datetime, at: datetime) -> str:
    seconds = max(0, int((now - at).total_seconds()))
    return f"{seconds // 60} min {seconds % 60} s ago" if seconds >= 60 else f"{seconds} s ago"


def _check_heartbeat(session: Any, service: str, fresh: timedelta, now: datetime) -> Check:
    from climate.store.app_settings import get_heartbeat

    hb = get_heartbeat(session, service)
    if hb is None:
        return Check(f"{service} heartbeat", "fail", "never seen; is the service running?")
    if now - hb.at > fresh:
        return Check(f"{service} heartbeat", "fail", f"last beat {_age(now, hb.at)} (stale)")
    if not hb.ok:
        return Check(f"{service} heartbeat", "fail", f"reports a problem ({_age(now, hb.at)})")
    return Check(f"{service} heartbeat", "ok", f"last beat {_age(now, hb.at)}")


def _source_check(session: Any, kind: str, now: datetime) -> Check:
    """Prefer the worker's view (it owns the source); never build an ecobee client here."""
    from climate.store.app_settings import get_heartbeat
    from climate.store.secrets import list_secret_keys

    hb = get_heartbeat(session, "worker")
    if hb is not None and now - hb.at <= WORKER_FRESH and "source_ok" in hb.detail and hb.detail.get("source") in (kind, None):
        if hb.detail.get("source_ok"):
            return Check("source", "ok", f"{kind}: the worker's last poll succeeded")
        return Check("source", "fail", f"{kind}: {hb.detail.get('source_error') or 'the worker cannot reach it'}")
    if kind == "ecobee":
        if "ecobee_refresh_token" in list_secret_keys(session, "ecobee_refresh_token"):
            return Check("source", "fail", "ecobee: signed in, but the worker is not reporting; start the worker")
        return Check("source", "fail", "ecobee: not signed in (Setup > ecobee)")

    async def probe() -> Any:
        from climate.sources import get_source

        source = get_source(kind)
        try:
            return await source.health()
        finally:
            await source.close()

    try:
        health = asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        from climate.collector.poller import safe_error

        return Check("source", "fail", f"{kind}: {safe_error(exc)}")
    return Check("source", "ok" if health.ok else "fail", f"{kind}: {health.detail or ('healthy' if health.ok else 'unhealthy')}")


def doctor_checks(now: datetime | None = None) -> list[Check]:
    from climate.timeutil import utcnow

    now = now or utcnow()
    checks: list[Check] = []

    if os.environ.get("ANTHROPIC_API_KEY"):
        checks.append(Check("ANTHROPIC_API_KEY", "fail", "is set in this environment; remove it (Claude runs on the "
                                                          "owner's subscription via CLAUDE_CODE_OAUTH_TOKEN in the agent only)"))
    else:
        checks.append(Check("ANTHROPIC_API_KEY", "ok", "not set"))

    from climate.config import get_settings

    cfg = get_settings()
    from cryptography.fernet import Fernet

    if not cfg.secret_key:
        key_check = Check("secret key", "fail", "CLIMATE_SECRET_KEY is not set (python -m climate.cli gen-key)")
    else:
        try:
            Fernet(cfg.secret_key.encode())
            key_check = Check("secret key", "ok", "valid Fernet key")
        except (ValueError, TypeError):
            key_check = Check("secret key", "fail", "CLIMATE_SECRET_KEY is not a valid Fernet key")
    checks.append(key_check)
    if not cfg.session_secret:
        checks.append(Check("session secret", "fail", "CLIMATE_SESSION_SECRET is not set; sign-ins reset on restart"))
    else:
        checks.append(Check("session secret", "ok", "set"))

    from climate.store.db import session_scope

    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        from climate.collector.poller import safe_error

        checks.append(Check("database", "fail", f"unreachable: {safe_error(exc)}"))
        return checks
    checks.append(Check("database", "ok", "reachable"))

    from climate.store.app_settings import AgentSettings, SourceSettings, get_setting
    from climate.store.migrate import _migration_files
    from climate.store.secrets import SecretsUnavailable, get_secret, list_secret_keys

    with session_scope() as s:
        try:
            done = set(s.execute(text("SELECT id FROM schema_migrations")).scalars())
        except Exception:  # noqa: BLE001
            s.rollback()
            done = set()
        pending = [mid for mid, _ in _migration_files() if mid not in done]
        checks.append(Check("migrations", "fail" if pending else "ok",
                            f"pending: {', '.join(pending)} (run: python -m climate.cli migrate)" if pending else "current"))
        if pending:
            return checks

        if key_check.status == "ok":
            stored = list_secret_keys(s)
            if stored:
                try:
                    get_secret(s, stored[0])
                    checks.append(Check("stored secrets", "ok", f"{len(stored)} decrypt with this key"))
                except SecretsUnavailable:
                    checks.append(Check("stored secrets", "fail", "cannot be decrypted with CLIMATE_SECRET_KEY "
                                                                  "(the key changed?)"))

        source = get_setting(s, "source", SourceSettings)
        agent = get_setting(s, "agent", AgentSettings)
        checks.append(_check_heartbeat(s, "worker", WORKER_FRESH, now))
        if source.homekit_enabled:
            checks.append(_check_heartbeat(s, "homekit", HOMEKIT_FRESH, now))
        else:
            checks.append(Check("homekit heartbeat", "skip", "HomeKit is not enabled"))
        if agent.enabled:
            checks.append(_check_heartbeat(s, "agent", AGENT_FRESH, now))
        else:
            checks.append(Check("agent heartbeat", "skip", "the Claude agent is disabled"))
    with session_scope() as s:
        checks.append(_source_check(s, source.kind, now))
    return checks


def cmd_doctor(_: argparse.Namespace) -> int:
    checks = doctor_checks()
    width = max(len(c.name) for c in checks)
    for c in checks:
        _print(f"{c.status.upper():4}  {c.name.ljust(width)}  {c.detail}")
    failed = [c for c in checks if c.status == "fail"]
    _print("")
    _print(f"{len(failed)} problem(s) found." if failed else "All checks passed.")
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m climate.cli", description="Climate AI admin commands.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="insert the house inventory and default settings").set_defaults(func=cmd_seed)
    b = sub.add_parser("backfill", help="load history from the configured source (ecobee: runtimeReport)")
    b.add_argument("--days", type=int, default=90, help="days of history to load (default 90)")
    b.set_defaults(func=cmd_backfill)
    sub.add_parser("gen-key", help="print a new CLIMATE_SECRET_KEY and CLIMATE_SESSION_SECRET").set_defaults(func=cmd_gen_key)
    sub.add_parser("set-password", help="set the owner's web password").set_defaults(func=cmd_set_password)
    sub.add_parser("doctor", help="check the installation; exit 1 on any problem").set_defaults(func=cmd_doctor)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    import logging

    from climate.config import get_settings

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("pyecobee").setLevel(logging.INFO)
    args = build_parser().parse_args(argv)
    get_settings()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

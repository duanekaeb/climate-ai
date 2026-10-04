"""Admin CLI: ``python -m climate.cli <command>``.

Commands: migrate | seed | backfill --days N | gen-key | set-password | create-token |
list-tokens | revoke-token | sign-out-everywhere | doctor | sim-event
(doctor checks DB, secrets key, source sign-in, HomeKit heartbeat, agent heartbeat).

The auth commands (set-password, create-token, list-tokens, revoke-token, sign-out-everywhere)
call the same ``climate.auth_service`` functions as the API, with audit actor 'system'
(docs/specs/users-and-tokens.md). ``create-token`` prints the token once; nothing else ever
prints a secret.

``backfill`` runs in this process with the configured source, except when the source is
ecobee and the worker is running: only the worker may refresh ecobee tokens, so the request
is queued as a 'backfill' job for it instead.

``sim-event`` puts a utility (demand-response) event on the simulated house. The simulator
lives in the worker's memory, so the event goes into app_settings['sim_events'], which the
worker's simulator re-reads every simulated minute (``climate.sources.simulator``).
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
    """New secrets for .env: the Fernet key, the access-token signing secret and the token
    pepper (docs/specs/users-and-tokens.md)."""
    from cryptography.fernet import Fernet

    _print(f"CLIMATE_SECRET_KEY={Fernet.generate_key().decode()}")
    _print(f"CLIMATE_JWT_SECRET={pysecrets.token_urlsafe(48)}")
    _print(f"CLIMATE_TOKEN_PEPPER={pysecrets.token_urlsafe(48)}")
    return 0


# ---------------------------------------------------------------------------------------
# the owner password, signed-in devices and API tokens (climate.auth_service; actor 'system')
# ---------------------------------------------------------------------------------------


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def cmd_set_password(args: argparse.Namespace, prompt: Callable[[str], str] = getpass.getpass) -> int:
    """Break-glass: set the owner password and sign out every device. ``--password-stdin``
    reads one line from standard input (for scripts); otherwise it asks twice."""
    from climate.auth_service import AuthError, set_owner_password
    from climate.config import get_settings

    if getattr(args, "password_stdin", False):
        first = sys.stdin.readline().rstrip("\r\n")
    else:
        first = prompt("New owner password: ")
        if prompt("Repeat it: ") != first:
            _err("The passwords do not match; nothing changed.")
            return 1
    try:
        revoked = set_owner_password(first)
    except AuthError as exc:
        _err(f"{exc.message} Nothing changed.")
        return 1
    _print(f"Owner password saved (Argon2id hash). Signed out {revoked} device(s); sign in again with the new one.")
    if get_settings().owner_password:
        _print("Note: CLIMATE_OWNER_PASSWORD is only read on the very first start; remove it from .env.")
    return 0


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "-"


def cmd_create_token(args: argparse.Namespace) -> int:
    """Create an API token and print it exactly once (only its HMAC is stored)."""
    from climate.auth_service import SYSTEM, AuthError, create_api_token

    try:
        row, raw = create_api_token(SYSTEM, name=args.name, role=args.role, expires_in_days=args.expires_days,
                                    local_only=not args.allow_remote)
    except AuthError as exc:
        _err(exc.message)
        return 1
    if args.print_only_token:
        _print(raw)
        return 0
    scope = "home network only" if row.local_only else "works from anywhere"
    expiry = f"expires {_when(row.expires_at)}" if row.expires_at else "no expiry"
    _print(f"Created API token {row.id} '{row.name}': role {row.role}, {scope}, {expiry}.")
    _print("")
    _print(f"  {raw}")
    _print("")
    _print("Copy it now. It is shown only this once and cannot be recovered; revoke it with "
           f"`python -m climate.cli revoke-token --id {row.id}` if it leaks.")
    return 0


def cmd_list_tokens(_: argparse.Namespace) -> int:
    """Every API token (no secrets: the hint is the last 4 characters)."""
    from climate.auth_service import list_api_tokens
    from climate.timeutil import utcnow

    rows = list_api_tokens()
    if not rows:
        _print("No API tokens. Create one with: python -m climate.cli create-token --name NAME")
        return 0
    now = utcnow()
    _print(f"{'ID':>4}  {'NAME':24}  {'ROLE':7}  {'SCOPE':8}  {'HINT':8}  {'LAST USED':20}  {'EXPIRES':20}  STATUS")
    for t in rows:
        if t.revoked_at is not None:
            state = "revoked"
        elif t.expires_at is not None and t.expires_at <= now:
            state = "expired"
        else:
            state = "active"
        _print(f"{t.id:>4}  {t.name[:24]:24}  {t.role:7}  {'home' if t.local_only else 'anywhere':8}  "
               f"{'...' + t.token_hint:8}  {_when(t.last_used_at):20}  {_when(t.expires_at):20}  {state}")
    return 0


def cmd_revoke_token(args: argparse.Namespace) -> int:
    from climate.auth_service import SYSTEM, revoke_api_token

    if revoke_api_token(SYSTEM, int(args.id)):
        _print(f"Revoked API token {args.id}; it stops working at once.")
        return 0
    _err(f"No active API token with id {args.id}.")
    return 1


def cmd_sign_out_everywhere(_: argparse.Namespace) -> int:
    """End every signed-in device (API tokens keep working; revoke those separately)."""
    from climate.auth_service import SYSTEM, logout_all

    n = logout_all(SYSTEM)
    _print(f"Signed out {n} device(s). API tokens are unaffected (list-tokens / revoke-token).")
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
# sim-event
# ---------------------------------------------------------------------------------------


def _sim_units(value: str | None) -> list[str]:
    from climate.sources.simulator import UNIT_KEYS as SIM_UNITS

    if not value or value.strip().lower() == "all":
        return list(SIM_UNITS)
    return [u.strip() for u in value.split(",") if u.strip()]


def cmd_sim_event(args: argparse.Namespace) -> int:
    """Inject a utility event into the simulator (or ``--clear`` the injected ones)."""
    from climate.sources.base import ThermostatEvent
    from climate.sources.simulator import add_sim_event, remove_sim_events
    from climate.store.app_settings import LocationSettings, SourceSettings, get_setting
    from climate.store.db import session_scope
    from climate.timeutil import to_local, utcnow
    from climate.utility.events import change_label

    now = utcnow().replace(second=0, microsecond=0)
    with session_scope() as s:
        if get_setting(s, "source", SourceSettings).kind != "simulator":
            _print("The data source is not the simulator; sim-event only works with the simulated house.")
            return 1
        tz = get_setting(s, "location", LocationSettings).tz
    units = _sim_units(args.unit)
    unknown = [u for u in units if u not in _sim_units("all")]
    if unknown or not units:
        _print(f"Unknown unit(s): {', '.join(unknown) or args.unit!r}; use main, up, bed, a comma-separated "
               f"list, or all.")
        return 1
    if args.clear:
        with session_scope() as s:
            removed = remove_sim_events(s, now, units)
        _print(f"Removed {removed} injected event(s) from {', '.join(units)}.")
        return 0
    if not args.unit:
        _print("--unit is required (main, up, bed, a comma-separated list, or all).")
        return 1
    relative = args.cool_offset is not None or args.heat_offset is not None
    absolute = args.cool is not None or args.heat is not None
    if relative == absolute:
        _print("Give offsets (--cool-offset / --heat-offset) or setpoints (--cool / --heat), not both and "
               "not neither.")
        return 1
    if args.hours <= 0:
        _print("--hours must be above 0.")
        return 1
    start = now + timedelta(minutes=args.in_min)
    event = ThermostatEvent(
        event_type="demandResponse", name=args.name, start=start, end=start + timedelta(hours=args.hours),
        heat_f=args.heat, cool_f=args.cool, is_relative=relative, heat_offset_f=args.heat_offset,
        cool_offset_f=args.cool_offset, is_optional=not args.mandatory,
    )
    try:
        with session_scope() as s:
            stored = add_sim_event(s, units, event, now)
    except ValueError as exc:
        _print(f"Not injected: {exc}")
        return 1
    assert stored.start is not None and stored.end is not None
    window = f"{to_local(stored.start, tz):%a %I:%M %p} to {to_local(stored.end, tz):%a %I:%M %p}"
    change = change_label(heat_f=stored.heat_f, cool_f=stored.cool_f, is_relative=stored.is_relative,
                          heat_offset_f=stored.heat_offset_f, cool_offset_f=stored.cool_offset_f)
    kind = "mandatory" if stored.is_optional is False else "optional"
    _print(f"Injected {kind} utility event {stored.name or '(no name)'!r} on {', '.join(units)}: {window} "
           f"({tz}), {change}. id {stored.link_ref}")
    if not _worker_fresh(utcnow()):
        _print("The worker is not running; the event takes effect once it is.")
    else:
        _print("The worker's simulator picks it up on its next poll (every minute by default).")
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


def _sign_in_secrets_check() -> Check:
    """CLIMATE_JWT_SECRET and CLIMATE_TOKEN_PEPPER: without them sign-in and every token check
    answer 503 AUTH_NOT_CONFIGURED (the same rule the API applies)."""
    from climate.api.security import config_problem

    problem = config_problem()
    if problem:
        return Check("sign-in secrets", "fail", f"{problem}; sign-in is off until then (scripts/bootstrap.sh fills them in)")
    return Check("sign-in secrets", "ok", "CLIMATE_JWT_SECRET and CLIMATE_TOKEN_PEPPER are set")


def _env_tokens_check(cfg: Any) -> Check | None:
    """CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN: say which kind each one holds (None when
    neither is set). A ``cai_`` value is a managed API token: it is checked live like any other,
    so revoking it (or letting it expire) in the app stops it, whatever .env still says."""
    from climate.auth_service import API_TOKEN_PREFIX

    notes = []
    for name, value in (("CLIMATE_AGENT_TOKEN", cfg.agent_token), ("CLIMATE_MCP_TOKEN", cfg.mcp_token)):
        value = (value or "").strip()
        if not value:
            continue
        if value.startswith(API_TOKEN_PREFIX):
            notes.append(f"{name} holds a managed API token (cai_): checked live, revocable in the app "
                         "(Security, API tokens)")
        else:
            notes.append(f"{name} is a legacy shared token (role agent, home network only; not revocable in the "
                         "app: prefer a cai_ API token)")
    return Check("env tokens", "ok", "; ".join(notes)) if notes else None


def _cookie_check(cfg: Any) -> Check:
    """The refresh cookie must be Secure whenever the app is reached over HTTPS."""
    if cfg.public_url.lower().startswith("https://") and not cfg.cookie_secure:
        return Check("cookies", "fail", "CLIMATE_PUBLIC_URL is https but CLIMATE_COOKIE_SECURE=false; set it to true so the "
                                        "sign-in cookie never travels over plain http")
    if cfg.cookie_secure:
        return Check("cookies", "ok", "Secure (sign-in needs HTTPS)")
    return Check("cookies", "ok", "not Secure: fine for plain http at home; set CLIMATE_COOKIE_SECURE=true behind HTTPS")


def _owner_password_check(session: Any, cfg: Any) -> Check:
    """Read-only: is the owner password stored? (Never imports CLIMATE_OWNER_PASSWORD here; the
    app does that when it starts.) Until it is chosen, whoever reaches the first-run screen first
    can choose it, so a missing password is a problem."""
    from climate.store.app_settings import OwnerSettings, get_setting

    encoded = get_setting(session, "owner", OwnerSettings).password_hash
    if encoded:
        if encoded.startswith("scrypt$"):
            return Check("owner password", "ok", "set (older scrypt hash; upgraded to Argon2id at the next sign-in)")
        return Check("owner password", "ok", "set (Argon2id hash)" if encoded.startswith("$argon2id$") else "set")
    if cfg.owner_password:
        detail = "not stored yet; CLIMATE_OWNER_PASSWORD is imported when the app starts (restart the app)"
    else:
        detail = ("not chosen yet: open the app from your home network and choose it, or run "
                  "python -m climate.cli set-password")
    if cfg.allow_remote_setup:
        detail += "; CLIMATE_ALLOW_REMOTE_SETUP is on, so anyone who reaches the app can choose it first"
    return Check("owner password", "fail", detail)


def doctor_checks(now: datetime | None = None) -> list[Check]:
    """Every installation check, in order: no ANTHROPIC_API_KEY, the Fernet key, the sign-in
    secrets, cookie security, the database, migrations, stored secrets decrypt, the owner
    password, the worker / HomeKit / agent heartbeats and the data source."""
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
    checks.append(_sign_in_secrets_check())
    checks.append(_cookie_check(cfg))
    env_tokens = _env_tokens_check(cfg)
    if env_tokens is not None:
        checks.append(env_tokens)

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

        checks.append(_owner_password_check(s, cfg))
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
    sub.add_parser("gen-key", help="print a new CLIMATE_SECRET_KEY, CLIMATE_JWT_SECRET and CLIMATE_TOKEN_PEPPER").set_defaults(
        func=cmd_gen_key)
    sp = sub.add_parser("set-password", help="set the owner password (break-glass; signs out every device)")
    sp.add_argument("--password-stdin", action="store_true", help="read the new password from standard input (one line)")
    sp.set_defaults(func=cmd_set_password)
    ct = sub.add_parser("create-token", help="create an API token for a service (printed once)")
    ct.add_argument("--name", required=True, help="what uses it, e.g. 'Claude agent'")
    ct.add_argument("--role", choices=("agent", "viewer", "control"), default="agent",
                    help="agent: read + gated agent tools; viewer: read only; control: read + everyday controls")
    ct.add_argument("--expires-days", type=int, help="days until it stops working (default: no expiry)")
    ct.add_argument("--allow-remote", action="store_true", help="also accept it from internet addresses "
                                                                "(default: home network / Tailscale only)")
    ct.add_argument("--print-only-token", action="store_true", help="print only the token (for scripts)")
    ct.set_defaults(func=cmd_create_token)
    sub.add_parser("list-tokens", help="list API tokens (no secrets)").set_defaults(func=cmd_list_tokens)
    rt = sub.add_parser("revoke-token", help="revoke an API token at once")
    rt.add_argument("--id", type=int, required=True, help="the token id (list-tokens)")
    rt.set_defaults(func=cmd_revoke_token)
    sub.add_parser("sign-out-everywhere", help="sign out every device (API tokens keep working)").set_defaults(
        func=cmd_sign_out_everywhere)
    sub.add_parser("doctor", help="check the installation; exit 1 on any problem").set_defaults(func=cmd_doctor)
    e = sub.add_parser("sim-event", help="put a utility (demand-response) event on the simulated house")
    e.add_argument("--unit", help="main, up, bed, a comma-separated list, or all (the same event on each)")
    e.add_argument("--in-min", type=int, default=30, help="minutes from now until it starts (default 30)")
    e.add_argument("--hours", type=float, default=2.0, help="how long it runs (default 2)")
    e.add_argument("--cool-offset", type=float, help="raise cooling by this many °F (e.g. 2)")
    e.add_argument("--heat-offset", type=float, help="lower heating by this many °F (negative, e.g. -2)")
    e.add_argument("--cool", type=float, help="absolute cooling setpoint °F (e.g. 78)")
    e.add_argument("--heat", type=float, help="absolute heating setpoint °F (e.g. 64)")
    e.add_argument("--name", default="Peak Saver", help="event name shown in the app (default 'Peak Saver')")
    e.add_argument("--mandatory", action="store_true", help="the utility allows no opt-out")
    e.add_argument("--clear", action="store_true", help="remove injected events (of --unit, default all)")
    e.set_defaults(func=cmd_sim_event)
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

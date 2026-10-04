"""The owner login, signed-in devices, API tokens, WebSocket tickets and the audit trail.

Spec: docs/specs/users-and-tokens.md. One login (the owner password), no user accounts.
Primitives (Argon2id, HMAC token hashes, JWTs, the private-address check) live in
``climate.api.security``; this module owns the database side and the brute-force guards.

Every public function opens and commits its OWN transaction (``session_scope``), so a failed
sign-in, a refresh-token reuse or a public-sign-in pause is recorded even though the call then
raises. Audit rows for successful actions are written in the same transaction as the action.
Errors are ``AuthError`` (status, code, message); the API layer raises ``err.to_http()``, which
is ``HTTPException(status, detail={"code", "message"})`` (``AuthErrorDetail``).

Function API (the auth-api builder codes against exactly these):

Types and constants
- ``class AuthError(Exception)``: ``.status: int``, ``.code: str``, ``.message: str``,
  ``.to_http() -> HTTPException``. Codes: NOT_AUTHENTICATED, TOKEN_EXPIRED, SESSION_REVOKED,
  INVALID_CREDENTIALS, TOO_MANY_ATTEMPTS, LOGIN_PAUSED, SETUP_NOT_ALLOWED, ALREADY_SET,
  PASSWORD_NOT_SET, WEAK_PASSWORD, FORBIDDEN, REAUTHENTICATION_REQUIRED, AUTH_NOT_CONFIGURED,
  INVALID_REQUEST. Wrong passwords: login 401, reauth / change-password 403 (still signed in).
- ``@dataclass(frozen=True) class Principal``: ``role`` ('owner' | 'agent' | 'viewer' |
  'control'), ``kind`` ('session' | 'api_token' | 'env_token' | 'system'), ``session_id: int |
  None``, ``token_id: int | None``, ``label: str`` (device / token name),
  ``reauthenticated_at: datetime | None``; property ``recently_authenticated -> bool``.
- ``SYSTEM: Principal`` (the CLI / break-glass actor; audit actor_type 'system').
- ``@dataclass(frozen=True) class Issued``: ``access_token: str``, ``expires_in: int``
  (seconds), ``session_id: int``, ``refresh_token: str`` (goes ONLY into the cookie),
  ``refresh_max_age: int`` (cookie Max-Age seconds).
- ``REFRESH_COOKIE = "climate_refresh"``, ``REFRESH_COOKIE_PATH = "/api/auth"``,
  ``WS_TICKET_TTL_S = 30``.

State
- ``auth_config_problem() -> str | None``: why sign-in cannot run (empty / short jwt_secret or
  token_pepper), or None. ``doctor`` uses it.
- ``password_set() -> bool``
- ``setup_allowed(ip: str | None) -> bool``: no password yet AND (private ip or allow_remote_setup).
- ``import_env_password() -> bool``: hash CLIMATE_OWNER_PASSWORD into the setting when none is
  set (call at startup; the sign-in functions call it too). True when it imported.

Sign-in and devices (owner)
- ``setup(password, *, ip, user_agent="", device_name="") -> Issued``
- ``login(password, *, ip, user_agent="", device_name="") -> Issued``
- ``refresh(refresh_token: str | None, *, ip, user_agent="") -> Issued``
- ``logout(refresh_token: str | None = None, *, principal: Principal | None = None, ip=None)
  -> None`` (never raises)
- ``logout_all(principal: Principal, *, ip=None) -> int`` (sessions revoked, this one too)
- ``reauth(principal: Principal, password: str, *, ip) -> Principal`` (fresh stamp)
- ``change_password(principal, current_password, new_password, *, ip) -> None``
- ``set_owner_password(password, *, actor: Principal = SYSTEM, ip=None) -> int`` (break-glass;
  revokes every session; returns how many)
- ``list_sessions() -> list[AuthSession]`` (open ones, newest first)
- ``revoke_session(principal, session_id: int, *, ip=None) -> bool``

Bearer checks (the resolver calls these on every request)
- ``resolve_bearer(token: str, *, ip: str | None) -> Principal``: a ``cai_`` API token (checked
  live even when it is also the value of an env token), a legacy env token
  (CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN holding a non-``cai_`` value, role agent, private
  addresses only) or an access JWT (re-checks the session row).
- ``check_access_token(token: str, *, ip: str | None = None) -> Principal``
- ``check_api_token(token: str, *, ip: str | None) -> Principal``
- ``principal_is_live(principal: Principal) -> bool`` (session / token still valid)

API tokens (owner; creating needs ``principal.recently_authenticated``)
- ``create_api_token(principal, *, name, role="agent", expires_in_days: int | None = None,
  local_only=True, ip=None) -> tuple[ApiToken, str]`` (the raw token, shown once)
- ``list_api_tokens() -> list[ApiToken]`` (newest first, revoked ones included)
- ``revoke_api_token(principal, token_id: int, *, ip=None) -> bool``

WebSocket tickets
- ``issue_ws_ticket(principal) -> tuple[str, int]`` (ticket, expires_in seconds)
- ``redeem_ws_ticket(ticket: str) -> Principal | None`` (single use; None when unknown,
  used, expired, or its session / token was revoked since)

Audit
- ``audit(session, actor: Principal | None, event_type, *, target_type=None, target_id=None,
  payload=None, ip=None) -> None`` (adds the row to the caller's transaction; never secrets)
- ``list_audit(event_type: str | None = None, limit: int = 100) -> list[AuditEvent]``

Tests
- ``reset_memory_state() -> None``: clears the in-memory limiter, pause cache and tickets.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.api.security import (
    AuthError,
    config_problem,
    constant_time_equals,
    decode_access_token,
    encode_access_token,
    hash_password,
    hash_token,
    is_private_address,
    new_secret,
    password_needs_rehash,
    require_configured,
    verify_password,
)
from climate.config import get_settings
from climate.store.app_settings import OwnerSettings, get_setting
from climate.store.db import session_scope
from climate.store.orm import ApiToken, AppSetting, AuditEvent, AuthSession
from climate.timeutil import utcnow

__all__ = [
    "REFRESH_COOKIE",
    "REFRESH_COOKIE_PATH",
    "SYSTEM",
    "TOKEN_ROLES",
    "WS_TICKET_TTL_S",
    "AuthError",
    "Issued",
    "Principal",
    "audit",
    "auth_config_problem",
    "change_password",
    "check_access_token",
    "check_api_token",
    "create_api_token",
    "import_env_password",
    "issue_ws_ticket",
    "list_api_tokens",
    "list_audit",
    "list_sessions",
    "login",
    "logout",
    "logout_all",
    "password_set",
    "principal_is_live",
    "reauth",
    "redeem_ws_ticket",
    "refresh",
    "reset_memory_state",
    "resolve_bearer",
    "revoke_api_token",
    "revoke_session",
    "set_owner_password",
    "setup",
    "setup_allowed",
]

log = logging.getLogger(__name__)

REFRESH_COOKIE = "climate_refresh"
REFRESH_COOKIE_PATH = "/api/auth"
WS_TICKET_TTL_S = 30
TOKEN_ROLES = ("agent", "viewer", "control")
API_TOKEN_PREFIX = "cai_"
# ids are lowercase hex (at most 15 digits, so they fit a BIGINT); secrets are token_urlsafe.
_REFRESH_RE = re.compile(r"([0-9a-f]{1,15})\.([A-Za-z0-9_-]{16,128})")
_API_TOKEN_RE = re.compile(r"cai_([0-9a-f]{1,15})_([A-Za-z0-9_-]{16,128})")

_PER_IP_LIMIT = 10  # failed password checks per client address per window
_PER_IP_WINDOW_S = 300
_MAX_CONCURRENT_CHECKS = 2  # Argon2 costs 64 MiB; never run more than two at once
_CHECK_QUEUE_S = 10.0
# A refresh whose reply never arrived (reload mid-refresh, dropped response) is retried with the
# token it replaced: accepted this long after a rotation, then treated as theft.
REFRESH_GRACE = timedelta(seconds=60)
_LAST_USED_EVERY = timedelta(minutes=1)  # API token last_used_* and session last_seen_at writes
_MAX_TICKETS = 10_000
_MAX_UA = 400
_MAX_DEVICE_NAME = 100

# Indirections so tests can move the clocks.
_monotonic = time.monotonic

_lock = threading.Lock()
_attempts: dict[str, deque[float]] = {}
_MAX_RETIRED = 100  # retired refresh-token hashes kept per session (reuse detection)


class _PublicBudget:
    """Password checks from public addresses, under ``_lock``: recent failures (monotonic
    stamps within ``public_login_window_minutes``) and checks in flight. A check reserves a
    place before it runs, so parallel guesses cannot overshoot ``public_login_max_failures``."""

    def __init__(self) -> None:
        self.failures: deque[float] = deque(maxlen=1000)
        self.inflight = 0


_public = _PublicBudget()
_checks = threading.BoundedSemaphore(_MAX_CONCURRENT_CHECKS)
_tickets: dict[str, tuple[float, Principal]] = {}


# ---------------------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who is calling. ``kind`` 'session' is the owner on a signed-in device; 'api_token' a
    ``cai_`` token; 'env_token' the legacy CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN; 'system'
    the CLI."""

    role: str
    kind: str
    session_id: int | None = None
    token_id: int | None = None
    label: str = ""
    reauthenticated_at: datetime | None = None

    @property
    def recently_authenticated(self) -> bool:
        """The owner typed the password on this device within ``reauth_window_minutes``
        (sign-in, setup and reauth stamp it). The CLI counts as recent; tokens never do."""
        if self.kind == "system":
            return True
        if self.kind != "session" or self.reauthenticated_at is None:
            return False
        return utcnow() - self.reauthenticated_at <= timedelta(minutes=get_settings().reauth_window_minutes)


SYSTEM = Principal(role="owner", kind="system", label="cli")


@dataclass(frozen=True)
class Issued:
    """A new or refreshed sign-in: the access token for the JSON body and the refresh token
    for the ``climate_refresh`` cookie only (``refresh_max_age`` seconds)."""

    access_token: str
    expires_in: int
    session_id: int
    refresh_token: str
    refresh_max_age: int


# ---------------------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------------------


def audit(
    session: Session, actor: Principal | None, event_type: str, *, target_type: str | None = None,
    target_id: str | int | None = None, payload: dict[str, Any] | None = None, ip: str | None = None,
) -> None:
    """Add an audit row to the caller's transaction. ``actor`` None = an unauthenticated
    attempt (failed sign-in). Payloads must never carry a password or a token."""
    if actor is None:
        actor_type, actor_id, label = "owner", None, "unauthenticated"
    elif actor.kind == "session":
        actor_type, actor_id, label = "owner", actor.session_id, actor.label
    elif actor.kind in ("api_token", "env_token"):
        actor_type, actor_id, label = "api_token", actor.token_id, actor.label
    else:
        actor_type, actor_id, label = "system", None, actor.label
    session.add(AuditEvent(
        ts=utcnow(), actor_type=actor_type, actor_id=actor_id, actor_label=label or "", event_type=event_type,
        target_type=target_type, target_id=None if target_id is None else str(target_id),
        payload=payload or {}, ip=ip,
    ))


def list_audit(event_type: str | None = None, limit: int = 100) -> list[AuditEvent]:
    """Newest first; ``limit`` is clamped to 1..500."""
    limit = max(1, min(int(limit), 500))
    stmt = select(AuditEvent).order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(limit)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    with session_scope() as s:
        return list(s.execute(stmt).scalars())


# ---------------------------------------------------------------------------------------
# the owner password
# ---------------------------------------------------------------------------------------


def auth_config_problem() -> str | None:
    return config_problem()


def _stored_hash(s: Session) -> str | None:
    return get_setting(s, "owner", OwnerSettings).password_hash


def _ensure_owner_row(s: Session) -> None:
    """Insert the empty owner setting if missing, never overwriting a concurrent claim."""
    s.execute(
        insert(AppSetting)
        .values(key="owner", value=OwnerSettings().model_dump(mode="json"), updated_by="seed")
        .on_conflict_do_nothing(index_elements=[AppSetting.key])
    )


def _claim(s: Session, encoded: str, updated_by: str) -> bool:
    """Atomically store the first password hash; False when one is already set."""
    _ensure_owner_row(s)
    won = s.execute(
        text(
            "UPDATE app_settings SET value = jsonb_set(value, '{password_hash}', to_jsonb(CAST(:h AS text))),"
            " updated_at = now(), updated_by = :by"
            " WHERE key = 'owner' AND (value->>'password_hash') IS NULL RETURNING key"
        ),
        {"h": encoded, "by": updated_by},
    ).first()
    return won is not None


def _store_hash(s: Session, encoded: str, updated_by: str) -> None:
    _ensure_owner_row(s)
    s.execute(
        text(
            "UPDATE app_settings SET value = jsonb_set(value, '{password_hash}', to_jsonb(CAST(:h AS text))),"
            " updated_at = now(), updated_by = :by WHERE key = 'owner'"
        ),
        {"h": encoded, "by": updated_by},
    )


def import_env_password() -> bool:
    """CLIMATE_OWNER_PASSWORD: hashed into the setting on first start, then ignored (changing
    or removing the variable later changes nothing; use set-password or the Security page)."""
    env_pw = get_settings().owner_password
    if not env_pw:
        return False
    with session_scope() as s:
        if _stored_hash(s) is not None:
            return False
    encoded = hash_password(env_pw)
    with session_scope() as s:
        won = _claim(s, encoded, "env")
        if won:
            audit(s, SYSTEM, "auth.setup", payload={"via": "CLIMATE_OWNER_PASSWORD"})
    if won:
        log.info("Imported CLIMATE_OWNER_PASSWORD as the owner password (stored as an Argon2id hash).")
    return won


def password_set() -> bool:
    with session_scope() as s:
        if _stored_hash(s) is not None:
            return True
    return import_env_password()


def setup_allowed(ip: str | None) -> bool:
    """First-run setup is possible: no password yet, and the caller is on a private address
    (or ``allow_remote_setup``)."""
    if password_set():
        return False
    return get_settings().allow_remote_setup or is_private_address(ip)


def _check_new_password(password: str) -> None:
    min_len = get_settings().password_min_length
    if len(password) < min_len:
        raise AuthError(422, "WEAK_PASSWORD", f"Choose a password of at least {min_len} characters.")
    if len(password) > 200:
        raise AuthError(422, "WEAK_PASSWORD", "The password may be at most 200 characters.")


# ---------------------------------------------------------------------------------------
# brute force
# ---------------------------------------------------------------------------------------


def _reserve_attempt(ip: str | None) -> float:
    """Reserve a password check for this client BEFORE checking (parallel guesses cannot slip
    through). Raises 429 TOO_MANY_ATTEMPTS past 10 failures in 5 minutes. A success gives the
    reservation back (``_forgive``), so only failures count."""
    key = ip or "unknown"
    with _lock:
        now = _monotonic()
        cutoff = now - _PER_IP_WINDOW_S
        if len(_attempts) > 1000:
            for k in [k for k, q in _attempts.items() if not q or q[-1] <= cutoff]:
                del _attempts[k]
        q = _attempts.setdefault(key, deque())
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= _PER_IP_LIMIT:
            raise AuthError(429, "TOO_MANY_ATTEMPTS", "Too many sign-in attempts; wait a few minutes and try again.")
        q.append(now)
        return now


def _forgive(ip: str | None, stamp: float) -> None:
    with _lock:
        q = _attempts.get(ip or "unknown")
        if q and stamp in q:
            q.remove(stamp)


def _paused_error(until: datetime | None) -> AuthError:
    """429 LOGIN_PAUSED: until the durable pause ends, or (None) a moment while the last
    guesses the budget allows are being checked."""
    if until is None:
        return AuthError(429, "LOGIN_PAUSED", "Sign-in from the internet is paused after too many wrong passwords; "
                                              "try again in a few minutes. At home or over Tailscale it still works.")
    minutes = max(1, int((until - utcnow()).total_seconds() // 60) + 1)
    return AuthError(429, "LOGIN_PAUSED", f"Sign-in from the internet is paused for about {minutes} min after "
                                         "too many wrong passwords. At home or over Tailscale it still works.")


def _raise_if_paused() -> None:
    """429 LOGIN_PAUSED while the durable pause (the ``auth.login_paused`` audit row) runs."""
    with session_scope() as s:
        until = _paused_until(s, utcnow())
    if until is not None:
        raise _paused_error(until)


def _public_failure_count(s: Session, now: datetime) -> int:
    """Failed password checks from public addresses in the window (the durable count)."""
    return s.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "auth.login_failed",
            AuditEvent.payload["public"].astext == "true",
            AuditEvent.ts > now - timedelta(minutes=get_settings().public_login_window_minutes),
        )
    ).scalar_one()


def _reserve_public(recorded: int) -> None:
    """Reserve a place in the public sign-in budget BEFORE the check runs (under the lock).

    ``recorded`` is the durable failure count (other processes, before a restart); the larger
    of it and this process's own recent failures counts. While checks are in flight, a new one
    is admitted only if failures + in-flight stay below the limit, so the last guess before
    the pause can never be joined by a parallel one; with nothing in flight one check runs
    (after a pause ends, one more failure starts the next pause at once)."""
    cfg = get_settings()
    with _lock:
        now = _monotonic()
        cutoff = now - cfg.public_login_window_minutes * 60
        q = _public.failures
        while q and q[0] <= cutoff:
            q.popleft()
        failures = max(len(q), recorded)
        if _public.inflight and failures + _public.inflight >= cfg.public_login_max_failures:
            raise _paused_error(None)
        _public.inflight += 1


def _release_public(failed: bool) -> None:
    """Give the reservation back; a failure moves into the recent failures in the same step."""
    with _lock:
        _public.inflight = max(0, _public.inflight - 1)
        if failed:
            _public.failures.append(_monotonic())


def _check_password(password: str, *, ip: str | None = None, public: bool = False, reason: str | None = None,
                    actor: Principal | None = None) -> bool:
    """One Argon2 verification (a dummy one when no password is set), at most two at once.
    Raises 429 TOO_MANY_ATTEMPTS when checks are queued for more than 10 s (a flood).

    A public check re-reads the pause once it holds a slot (a pause may have started while it
    queued). With ``reason``, a wrong password is recorded (and the pause written, when it is
    due) BEFORE the slot is released, so the next queued check sees it."""
    if not _checks.acquire(timeout=_CHECK_QUEUE_S):
        raise AuthError(429, "TOO_MANY_ATTEMPTS", "The server is busy checking passwords; try again shortly.")
    try:
        if public:
            _raise_if_paused()
        with session_scope() as s:
            stored = _stored_hash(s)
        ok = verify_password(password, stored)
        if not ok and reason is not None:
            _record_failure(ip, public, reason, actor)
        return ok
    finally:
        _checks.release()


def _paused_until(s: Session, now: datetime) -> datetime | None:
    last = s.execute(
        select(func.max(AuditEvent.ts)).where(AuditEvent.event_type == "auth.login_paused")
    ).scalar_one_or_none()
    if last is None:
        return None
    until = last + timedelta(minutes=get_settings().login_pause_minutes)
    return until if until > now else None


def _record_failure(ip: str | None, public: bool, reason: str, actor: Principal | None = None) -> None:
    """Audit a failed password check in its own transaction (the caller then raises). Failures
    from public addresses are counted globally; past the limit, internet sign-in pauses."""
    from climate.notify import raise_alert

    cfg = get_settings()
    now = utcnow()
    with session_scope() as s:
        audit(s, actor, "auth.login_failed", payload={"reason": reason, "public": public}, ip=ip)
        if not public:
            return
        s.flush()
        failures = _public_failure_count(s, now)
        if failures < cfg.public_login_max_failures or _paused_until(s, now) is not None:
            return
        audit(s, SYSTEM, "auth.login_paused", payload={
            "failures": failures, "window_minutes": cfg.public_login_window_minutes,
            "pause_minutes": cfg.login_pause_minutes,
        }, ip=ip)
        raise_alert(
            s, "auth:login_paused", "warn", "Sign-in from the internet is paused",
            f"{failures} wrong passwords from internet addresses in {cfg.public_login_window_minutes} min. "
            f"Sign-in from outside the home network is paused for {cfg.login_pause_minutes} min; at home or "
            "over Tailscale it still works, and signed-in devices are unaffected.",
            dedupe_key="auth:login_paused",
        )
        log.warning("Public sign-in paused for %s min after %s failures", cfg.login_pause_minutes, failures)


def _guarded_check(password: str, ip: str | None, reason: str, actor: Principal | None = None) -> bool:
    """Every password check (sign-in, reauth, change-password) goes through here: from a
    public address the internet pause applies (429 LOGIN_PAUSED) and the check reserves a
    place in the public budget; then the per-address reservation and the Argon2 check.
    Failures are audited and counted."""
    public = not is_private_address(ip)
    recorded = 0
    if public:
        now = utcnow()
        with session_scope() as s:
            until = _paused_until(s, now)
            recorded = 0 if until is not None else _public_failure_count(s, now)
        if until is not None:
            raise _paused_error(until)
    stamp = _reserve_attempt(ip)
    try:
        ok = _budgeted_check(password, ip, public, reason, actor, recorded)
    except AuthError:
        _forgive(ip, stamp)
        raise
    if ok:
        _forgive(ip, stamp)
    return ok


def _budgeted_check(password: str, ip: str | None, public: bool, reason: str, actor: Principal | None,
                    recorded: int) -> bool:
    if not public:
        return _check_password(password, ip=ip, reason=reason, actor=actor)
    _reserve_public(recorded)
    failed = False
    try:
        ok = _check_password(password, ip=ip, public=True, reason=reason, actor=actor)
        failed = not ok
        return ok
    finally:
        _release_public(failed)


def _maybe_rehash(password: str) -> None:
    """Upgrade a legacy (scrypt) or weaker hash after a successful check, compare-and-swap."""
    with session_scope() as s:
        stored = _stored_hash(s)
        if not stored or not password_needs_rehash(stored):
            return
        s.execute(
            text(
                "UPDATE app_settings SET value = jsonb_set(value, '{password_hash}', to_jsonb(CAST(:new AS text))),"
                " updated_at = now(), updated_by = 'owner' WHERE key = 'owner' AND value->>'password_hash' = :old"
            ),
            {"new": hash_password(password), "old": stored},
        )


# ---------------------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------------------


def _clean(value: str, limit: int) -> str:
    """Printable characters only, whitespace collapsed, cut to ``limit``."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in (value or "")).split())[:limit]


def _device_name(device_name: str, user_agent: str) -> str:
    """The name the owner typed, or a short one guessed from the user agent ("iPhone")."""
    name = _clean(device_name, _MAX_DEVICE_NAME)
    if name:
        return name
    ua = user_agent.lower()
    for needle, label in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("macintosh", "Mac"),
                          ("windows", "Windows"), ("cros", "Chromebook"), ("linux", "Linux")):
        if needle in ua:
            return label
    return "Browser" if ua else "Unknown device"


def _is_open(row: AuthSession, now: datetime) -> bool:
    return row.revoked_at is None and row.expires_at > now and row.absolute_expires_at > now


def _open_session_filter(now: datetime) -> tuple[Any, ...]:
    return (AuthSession.revoked_at.is_(None), AuthSession.expires_at > now, AuthSession.absolute_expires_at > now)


def _owner_principal(row: AuthSession) -> Principal:
    return Principal(role="owner", kind="session", session_id=row.id, label=row.device_name,
                     reauthenticated_at=row.reauthenticated_at)


def _new_session(s: Session, now: datetime, *, ip: str | None, user_agent: str, device_name: str) -> tuple[AuthSession, str]:
    """Insert a signed-in device (password just entered, so the reauth stamp is now)."""
    cfg = get_settings()
    absolute = now + timedelta(days=cfg.session_max_days)
    row = AuthSession(
        refresh_token_hash=f"pending:{new_secret()}", previous_token_hash=None, retired_token_hashes=[],
        family_id=uuid.uuid4().hex,
        rotation_counter=0, device_name=_device_name(device_name, user_agent), user_agent=_clean(user_agent, _MAX_UA),
        ip=ip, created_at=now, last_seen_at=now, expires_at=min(now + timedelta(days=cfg.refresh_ttl_days), absolute),
        absolute_expires_at=absolute, reauthenticated_at=now,
    )
    s.add(row)
    s.flush()
    raw = f"{row.id:x}.{new_secret()}"
    row.refresh_token_hash = hash_token(raw)
    return row, raw


def _issued(row: AuthSession, raw_refresh: str, now: datetime) -> Issued:
    access, ttl = encode_access_token(row.id, now)
    max_age = max(0, int((row.expires_at - now).total_seconds()))
    return Issued(access_token=access, expires_in=ttl, session_id=row.id, refresh_token=raw_refresh,
                  refresh_max_age=max_age)


def _revoke_sessions(s: Session, now: datetime, reason: str, *, keep: int | None = None,
                     only: int | None = None, family: str | None = None) -> int:
    stmt = update(AuthSession).where(AuthSession.revoked_at.is_(None))
    if keep is not None:
        stmt = stmt.where(AuthSession.id != keep)
    if only is not None:
        stmt = stmt.where(AuthSession.id == only)
    if family is not None:
        stmt = stmt.where(AuthSession.family_id == family)
    result = s.execute(stmt.values(revoked_at=now, revoked_reason=reason).returning(AuthSession.id))
    return len(result.all())


def setup(password: str, *, ip: str | None, user_agent: str = "", device_name: str = "") -> Issued:
    """First run: choose the owner password and sign this device in. Only while no password
    is set (atomic claim) and only from a private address unless ``allow_remote_setup``."""
    require_configured()
    if password_set():
        raise AuthError(409, "ALREADY_SET", "A password is already set; sign in instead.")
    if not (get_settings().allow_remote_setup or is_private_address(ip)):
        raise AuthError(403, "SETUP_NOT_ALLOWED",
                        "Choose the password from your home network (or Tailscale) the first time.")
    _check_new_password(password)
    encoded = hash_password(password)
    now = utcnow()
    with session_scope() as s:
        if not _claim(s, encoded, "owner"):
            raise AuthError(409, "ALREADY_SET", "A password is already set; sign in instead.")
        row, raw = _new_session(s, now, ip=ip, user_agent=user_agent, device_name=device_name)
        audit(s, _owner_principal(row), "auth.setup", target_type="session", target_id=row.id, ip=ip)
        return _issued(row, raw, now)


def login(password: str, *, ip: str | None, user_agent: str = "", device_name: str = "") -> Issued:
    """Sign in with the owner password and open a new device session.

    Raises 401 INVALID_CREDENTIALS (wrong password), 429 TOO_MANY_ATTEMPTS (this address),
    429 LOGIN_PAUSED (internet sign-in paused; home / Tailscale always works), 409
    PASSWORD_NOT_SET (first run not done), 503 AUTH_NOT_CONFIGURED."""
    require_configured()
    if not password_set():
        _check_password(password)  # the same Argon2 cost as a real check (dummy hash)
        raise AuthError(409, "PASSWORD_NOT_SET", "No password is set yet; choose one first.")
    if not _guarded_check(password, ip, "wrong_password"):
        raise AuthError(401, "INVALID_CREDENTIALS", "Wrong password.")
    _maybe_rehash(password)
    now = utcnow()
    with session_scope() as s:
        row, raw = _new_session(s, now, ip=ip, user_agent=user_agent, device_name=device_name)
        audit(s, _owner_principal(row), "auth.login", target_type="session", target_id=row.id,
              payload={"device_name": row.device_name}, ip=ip)
        return _issued(row, raw, now)


def _parse_refresh(token: str | None) -> int | None:
    """The session id of ``"<id hex>.<secret>"``, or None when malformed."""
    match = _REFRESH_RE.fullmatch(token or "")
    return int(match.group(1), 16) if match else None


def _matches_any(digest: str, hashes: list[str] | None) -> bool:
    """Constant-time membership: every stored hash is compared (no early exit)."""
    return any([constant_time_equals(digest, h) for h in hashes or []])  # noqa: C419 (no short-circuit on purpose)


def refresh(refresh_token: str | None, *, ip: str | None, user_agent: str = "") -> Issued:
    """Rotate the refresh token and mint a new access token.

    The secret is checked BEFORE anything about the session is revealed: a token that matches
    none of the session's current, previous or retired hashes (a forgery, or a guessed id) is
    401 NOT_AUTHENTICATED and changes nothing; only a genuine one learns SESSION_REVOKED.

    The presented token must be the session's current one. Presenting the PREVIOUS one within
    ``REFRESH_GRACE`` of the last rotation is a lost response (a reload mid-refresh, a dropped
    reply on a phone) or two tabs racing: it rotates again instead of signing the device out,
    and the holder of the newer token is caught by the rule below if it was a thief. Presenting
    any other retired one (the previous one after the grace, or an older one: a thief who
    rotated more than once) revokes the session family and raises 401 SESSION_REVOKED (audited
    as ``auth.refresh_reuse``). Every retired hash is kept (the last ``_MAX_RETIRED``). Sliding
    expiry: each refresh extends the session by ``refresh_ttl_days``, never past
    ``session_max_days`` from sign-in."""
    require_configured()
    sid = _parse_refresh(refresh_token)
    if sid is None or refresh_token is None:
        raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.")
    digest = hash_token(refresh_token)
    cfg = get_settings()
    now = utcnow()
    reused = False
    with session_scope() as s:
        row = s.get(AuthSession, sid, with_for_update=True)
        if row is None:
            raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.")
        current = constant_time_equals(digest, row.refresh_token_hash)
        previous = row.previous_token_hash is not None and constant_time_equals(digest, row.previous_token_hash)
        retired = _matches_any(digest, row.retired_token_hashes)
        if not (current or previous or retired):
            raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.")
        if row.revoked_at is not None:
            raise AuthError(401, "SESSION_REVOKED", "This device was signed out; sign in again.")
        grace = (
            not current and previous and row.rotated_at is not None and now - row.rotated_at <= REFRESH_GRACE
        )
        if current or grace:
            if not _is_open(row, now):
                raise AuthError(401, "SESSION_REVOKED", "This sign-in has expired; sign in again.")
            raw = f"{row.id:x}.{new_secret()}"
            row.retired_token_hashes = [*(row.retired_token_hashes or []), row.refresh_token_hash][-_MAX_RETIRED:]
            row.previous_token_hash = row.refresh_token_hash
            row.refresh_token_hash = hash_token(raw)
            row.rotation_counter += 1
            row.rotated_at = now
            row.expires_at = min(now + timedelta(days=cfg.refresh_ttl_days), row.absolute_expires_at)
            row.last_seen_at = now
            row.ip = ip
            if user_agent:
                row.user_agent = _clean(user_agent, _MAX_UA)
            return _issued(row, raw, now)
        revoked = _revoke_sessions(s, now, "reuse_detected", family=row.family_id)
        audit(s, None, "auth.refresh_reuse", target_type="session", target_id=row.id,
              payload={"family_revoked": revoked, "rotation_counter": row.rotation_counter,
                       "reused": "previous" if previous else "older"}, ip=ip)
        reused = True
    if reused:
        log.warning("Refresh token reuse on session %s; the session was revoked", sid)
        raise AuthError(401, "SESSION_REVOKED", "This sign-in was used from somewhere else and has been ended; "
                                                "sign in again.")
    raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.")


def logout(refresh_token: str | None = None, *, principal: Principal | None = None, ip: str | None = None) -> None:
    """Sign this device out: the session named by the bearer principal and/or the refresh
    cookie (which must be the session's current token). Never raises; unknown input is a
    no-op."""
    targets: set[int] = set()
    if principal is not None and principal.kind == "session" and principal.session_id is not None:
        targets.add(principal.session_id)
    sid = _parse_refresh(refresh_token)
    now = utcnow()
    with session_scope() as s:
        if sid is not None and refresh_token is not None and config_problem() is None:
            row = s.get(AuthSession, sid)
            if row is not None and constant_time_equals(hash_token(refresh_token), row.refresh_token_hash):
                targets.add(sid)
        for target in sorted(targets):
            row = s.get(AuthSession, target)
            if row is None or row.revoked_at is not None:
                continue
            _revoke_sessions(s, now, "logout", only=target)
            actor = principal if principal is not None and principal.session_id == target else _owner_principal(row)
            audit(s, actor, "auth.logout", target_type="session", target_id=target, ip=ip)


def logout_all(principal: Principal, *, ip: str | None = None) -> int:
    """Sign out every device, this one too. Returns how many sessions ended."""
    with session_scope() as s:
        n = _revoke_sessions(s, utcnow(), "logout_all")
        audit(s, principal, "auth.logout_all", payload={"sessions_revoked": n}, ip=ip)
    return n


def _require_owner_session(principal: Principal) -> None:
    if principal.kind != "session" or principal.session_id is None:
        raise AuthError(403, "FORBIDDEN", "Only the owner, signed in on a device, can do this.")


def reauth(principal: Principal, password: str, *, ip: str | None) -> Principal:
    """Re-enter the password on this device; stamps it for ``reauth_window_minutes``.
    A wrong password is 403 INVALID_CREDENTIALS (the device stays signed in); from a public
    address while internet sign-in is paused, 429 LOGIN_PAUSED (like every password check)."""
    _require_owner_session(principal)
    if not _guarded_check(password, ip, "reauth", principal):
        raise AuthError(403, "INVALID_CREDENTIALS", "Wrong password.")
    _maybe_rehash(password)
    now = utcnow()
    with session_scope() as s:
        row = s.get(AuthSession, principal.session_id, with_for_update=True)
        if row is None or not _is_open(row, now):
            raise AuthError(401, "SESSION_REVOKED", "This device was signed out; sign in again.")
        row.reauthenticated_at = now
        row.last_seen_at = now
        audit(s, principal, "auth.reauth", target_type="session", target_id=row.id, ip=ip)
        return replace(_owner_principal(row), reauthenticated_at=now)


def change_password(principal: Principal, current_password: str, new_password: str, *, ip: str | None) -> None:
    """Change the owner password (the current one is required). Every OTHER signed-in device
    is signed out; this one stays and counts as recently authenticated. API tokens stay.
    From a public address while internet sign-in is paused: 429 LOGIN_PAUSED."""
    _require_owner_session(principal)
    _check_new_password(new_password)
    if not _guarded_check(current_password, ip, "change_password", principal):
        raise AuthError(403, "INVALID_CREDENTIALS", "The current password is wrong.")
    encoded = hash_password(new_password)
    now = utcnow()
    with session_scope() as s:
        row = s.get(AuthSession, principal.session_id, with_for_update=True)
        if row is None or not _is_open(row, now):
            raise AuthError(401, "SESSION_REVOKED", "This device was signed out; sign in again.")
        _store_hash(s, encoded, "owner")
        revoked = _revoke_sessions(s, now, "password_change", keep=row.id)
        row.reauthenticated_at = now
        audit(s, principal, "password.change", payload={"sessions_revoked": revoked}, ip=ip)


def set_owner_password(password: str, *, actor: Principal = SYSTEM, ip: str | None = None) -> int:
    """Break-glass (``climate.cli set-password``): set the password and sign out EVERY device.
    Returns how many sessions ended."""
    _check_new_password(password)
    encoded = hash_password(password)
    with session_scope() as s:
        _store_hash(s, encoded, actor.kind if actor.kind == "system" else "owner")
        revoked = _revoke_sessions(s, utcnow(), "password_reset")
        audit(s, actor, "password.change", payload={"via": "cli" if actor.kind == "system" else "api",
                                                    "sessions_revoked": revoked}, ip=ip)
    return revoked


def list_sessions() -> list[AuthSession]:
    """Open (signed-in) devices, most recently seen first."""
    now = utcnow()
    with session_scope() as s:
        return list(s.execute(
            select(AuthSession).where(*_open_session_filter(now)).order_by(AuthSession.last_seen_at.desc())
        ).scalars())


def revoke_session(principal: Principal, session_id: int, *, ip: str | None = None) -> bool:
    """Sign one device out. False when it does not exist or is already signed out."""
    with session_scope() as s:
        if not _revoke_sessions(s, utcnow(), "revoked_by_owner", only=session_id):
            return False
        audit(s, principal, "session.revoke", target_type="session", target_id=session_id, ip=ip)
    return True


# ---------------------------------------------------------------------------------------
# bearer checks
# ---------------------------------------------------------------------------------------


def check_access_token(token: str, *, ip: str | None = None) -> Principal:
    """Verify an access JWT and re-check its session row (revoked or expired: 401
    SESSION_REVOKED), so sign-out and password changes apply at once. ``last_seen_at`` (and
    the device's address) are written at most once a minute."""
    claims = decode_access_token(token)
    now = utcnow()
    with session_scope() as s:
        row = s.get(AuthSession, claims["sid"])
        if row is None or not _is_open(row, now):
            raise AuthError(401, "SESSION_REVOKED", "This device was signed out; sign in again.")
        if now - row.last_seen_at >= _LAST_USED_EVERY:
            row.last_seen_at = now
            if ip:
                row.ip = ip
        return _owner_principal(row)


def _parse_api_token(token: str) -> int | None:
    """The token id of ``"cai_<id hex>_<secret>"``, or None when malformed."""
    match = _API_TOKEN_RE.fullmatch(token or "")
    return int(match.group(1), 16) if match else None


def check_api_token(token: str, *, ip: str | None) -> Principal:
    """Verify a ``cai_`` token live: known, not revoked, not expired, and (``local_only``)
    from a private address. ``last_used_at`` / ``last_used_ip`` are written at most once a
    minute."""
    require_configured()
    tid = _parse_api_token(token)
    if tid is None:
        raise AuthError(401, "NOT_AUTHENTICATED", "Unknown API token.")
    digest = hash_token(token)
    now = utcnow()
    with session_scope() as s:
        row = s.get(ApiToken, tid)
        if row is None or not constant_time_equals(digest, row.token_hash):
            raise AuthError(401, "NOT_AUTHENTICATED", "Unknown API token.")
        if row.revoked_at is not None:
            raise AuthError(401, "NOT_AUTHENTICATED", "This API token has been revoked.")
        if row.expires_at is not None and row.expires_at <= now:
            raise AuthError(401, "NOT_AUTHENTICATED", "This API token has expired.")
        if row.local_only and not is_private_address(ip):
            raise AuthError(403, "FORBIDDEN", "This API token only works from the home network.")
        if row.last_used_at is None or now - row.last_used_at >= _LAST_USED_EVERY:
            row.last_used_at = now
            row.last_used_ip = ip
        return Principal(role=row.role, kind="api_token", token_id=row.id, label=row.name)


def _legacy_env_principal(token: str, ip: str | None) -> Principal | None:
    """CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN: role agent, constant-time, private only.

    A ``cai_`` value is never a legacy token, whether presented or configured: a managed API
    token pasted into .env must still be checked live (``check_api_token``), so revoking or
    expiring it in the app takes effect."""
    if token.startswith(API_TOKEN_PREFIX):
        return None
    cfg = get_settings()
    for label, configured in (("CLIMATE_AGENT_TOKEN", cfg.agent_token), ("CLIMATE_MCP_TOKEN", cfg.mcp_token)):
        configured = (configured or "").strip()
        if configured and not configured.startswith(API_TOKEN_PREFIX) and constant_time_equals(token, configured):
            if not is_private_address(ip):
                raise AuthError(403, "FORBIDDEN", "This token only works from the home network.")
            return Principal(role="agent", kind="env_token", label=label)
    return None


def resolve_bearer(token: str, *, ip: str | None) -> Principal:
    """Who an ``Authorization: Bearer`` value belongs to: a ``cai_`` API token (always checked
    live, even when the same value sits in CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN), a legacy
    env token or an owner access token. Raises AuthError otherwise."""
    token = (token or "").strip()
    if not token:
        raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.")
    legacy = _legacy_env_principal(token, ip)
    if legacy is not None:
        return legacy
    if token.startswith(API_TOKEN_PREFIX):
        return check_api_token(token, ip=ip)
    return check_access_token(token, ip=ip)


def principal_is_live(principal: Principal) -> bool:
    """The session / token behind a principal is still valid (for long-lived uses such as a
    WebSocket opened with a ticket)."""
    now = utcnow()
    if principal.kind == "session":
        with session_scope() as s:
            row = s.get(AuthSession, principal.session_id)
            return row is not None and _is_open(row, now)
    if principal.kind == "api_token":
        with session_scope() as s:
            tok = s.get(ApiToken, principal.token_id)
            return tok is not None and tok.revoked_at is None and (tok.expires_at is None or tok.expires_at > now)
    return True


# ---------------------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------------------


def create_api_token(
    principal: Principal, *, name: str, role: str = "agent", expires_in_days: int | None = None,
    local_only: bool = True, ip: str | None = None,
) -> tuple[ApiToken, str]:
    """Create a ``cai_<id hex>_<secret>`` token. Returns (row, raw token): the raw token is
    shown once and never stored (only its HMAC). Needs the owner with the password re-entered
    recently (403 REAUTHENTICATION_REQUIRED), or the CLI."""
    require_configured()
    if principal.kind not in ("session", "system") or principal.role != "owner":
        raise AuthError(403, "FORBIDDEN", "Only the owner can create API tokens.")
    if not principal.recently_authenticated:
        raise AuthError(403, "REAUTHENTICATION_REQUIRED", "Enter your password again to create a token.")
    if role not in TOKEN_ROLES:
        raise AuthError(422, "INVALID_REQUEST", f"The role must be one of {', '.join(TOKEN_ROLES)}.")
    clean_name = _clean(name, 101)
    if not 1 <= len(clean_name) <= 100:
        raise AuthError(422, "INVALID_REQUEST", "Give the token a name of 1 to 100 characters.")
    if expires_in_days is not None and not 1 <= int(expires_in_days) <= 3650:
        raise AuthError(422, "INVALID_REQUEST", "The expiry must be 1 to 3650 days (or none).")
    now = utcnow()
    expires_at = now + timedelta(days=int(expires_in_days)) if expires_in_days else None
    with session_scope() as s:
        row = ApiToken(name=clean_name, token_hint="", token_hash=f"pending:{new_secret()}", role=role,
                       local_only=bool(local_only), created_at=now, expires_at=expires_at)
        s.add(row)
        s.flush()
        raw = f"{API_TOKEN_PREFIX}{row.id:x}_{new_secret()}"
        row.token_hash = hash_token(raw)
        row.token_hint = raw[-4:]
        audit(s, principal, "token.create", target_type="api_token", target_id=row.id, payload={
            "name": clean_name, "role": role, "local_only": bool(local_only),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }, ip=ip)
    return row, raw


def list_api_tokens() -> list[ApiToken]:
    with session_scope() as s:
        return list(s.execute(select(ApiToken).order_by(ApiToken.id.desc())).scalars())


def revoke_api_token(principal: Principal, token_id: int, *, ip: str | None = None) -> bool:
    """Revoke a token at once. False when it does not exist or is already revoked."""
    now = utcnow()
    with session_scope() as s:
        row = s.get(ApiToken, token_id, with_for_update=True)
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = now
        row.revoked_reason = "revoked_by_owner" if principal.kind != "system" else "revoked_by_cli"
        audit(s, principal, "token.revoke", target_type="api_token", target_id=row.id,
              payload={"name": row.name, "role": row.role}, ip=ip)
    return True


# ---------------------------------------------------------------------------------------
# WebSocket tickets
# ---------------------------------------------------------------------------------------


def issue_ws_ticket(principal: Principal) -> tuple[str, int]:
    """A single-use ticket for ``/api/ws?ticket=`` (browsers cannot send a bearer header on a
    WebSocket). Lives 30 s in this process's memory and carries the caller's role and
    session / token id."""
    ticket = secrets.token_urlsafe(32)
    with _lock:
        now = _monotonic()
        for key in [k for k, (exp, _) in _tickets.items() if exp <= now]:
            del _tickets[key]
        while len(_tickets) >= _MAX_TICKETS:
            del _tickets[next(iter(_tickets))]
        _tickets[ticket] = (now + WS_TICKET_TTL_S, principal)
    return ticket, WS_TICKET_TTL_S


def redeem_ws_ticket(ticket: str) -> Principal | None:
    """Use a ticket (once). None when unknown, already used, expired, or when its session or
    token was revoked since it was issued."""
    if not ticket:
        return None
    with _lock:
        entry = _tickets.pop(ticket, None)
        now = _monotonic()
    if entry is None or entry[0] <= now:
        return None
    principal = entry[1]
    return principal if principal_is_live(principal) else None


def reset_memory_state() -> None:
    """Tests: forget the per-address limiter, the public sign-in budget and every WebSocket
    ticket."""
    with _lock:
        _attempts.clear()
        _tickets.clear()
        _public.failures.clear()
        _public.inflight = 0

"""Who is calling, and what they may do. Spec: docs/specs/users-and-tokens.md.

One login (the owner password, no user accounts) plus API tokens for services. Every
authenticated call carries ``Authorization: Bearer <token>``; there is no cookie auth outside
``/api/auth/refresh`` and ``/api/auth/logout`` (``routers/auth.py``). The resolver hands the
bearer to ``climate.auth_service.resolve_bearer``, which recognises:

- ``cai_<id hex>_<secret>``: an API token (role ``agent``, ``viewer`` or ``control``), checked
  live on every use (revoked, expired) and refused from public addresses when ``local_only``;
- the legacy ``CLIMATE_AGENT_TOKEN`` / ``CLIMATE_MCP_TOKEN``: role ``agent``, constant-time
  compared, private addresses only;
- an access JWT: the owner, with the session row re-checked so sign-out and password changes
  take effect at once.

"Address" is always ``request.client.host``: the client after uvicorn's trusted proxy headers
(``--proxy-headers --forwarded-allow-ips``), never a raw ``X-Forwarded-For`` an attacker could set.

Roles and the dependencies that admit them (the route-guard test pins this for every route):

========================  ================================  ==========================================
Dependency                Allowed                           Used for
========================  ================================  ==========================================
``ReaderDep``             owner, agent, viewer, control     reads
``ControlDep``            owner, control                    everyday controls (holds, resume, presence,
                                                            back to automatic, utility-event skips)
``OwnerDep``              owner                             everything else that changes state
``RecentAuthDep``         owner, password re-entered within creating API tokens
                          ``reauth_window_minutes``
``WriterDep``             owner, agent                      the gated agent writes (propose, sign off)
``AgentDep``              agent                             the agent's own run bookkeeping
========================  ================================  ==========================================

``ReaderDep`` / ``ControlDep`` / ``OwnerDep`` / ``WriterDep`` / ``AgentDep`` resolve to the
caller's ``Role``; ``CallerDep`` / ``OwnerCallerDep`` / ``RecentAuthDep`` resolve to the full
``Principal`` (session id, token id, label) for routes that need it. Auth failures are
``HTTPException(status, detail={"code", "message"})`` (``AuthErrorDetail``): 401
NOT_AUTHENTICATED / TOKEN_EXPIRED / SESSION_REVOKED, 403 FORBIDDEN / REAUTHENTICATION_REQUIRED,
503 AUTH_NOT_CONFIGURED.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import cast
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from starlette.requests import HTTPConnection

from climate import auth_service
from climate.api.schemas import Role
from climate.api.security import AuthError, is_private_address, verify_password  # verify_password: re-exported for older callers
from climate.auth_service import Principal
from climate.config import get_settings

__all__ = [
    "AgentDep",
    "AuthError",
    "CallerDep",
    "ControlDep",
    "OwnerCallerDep",
    "OwnerDep",
    "Principal",
    "ReaderDep",
    "RecentAuthDep",
    "Role",
    "WriterDep",
    "auth_error",
    "bearer_token",
    "client_ip",
    "optional_caller",
    "resolve_caller",
    "verify_password",
]

ALL_ROLES: frozenset[str] = frozenset({"owner", "agent", "viewer", "control"})
_STATE_ATTR = "climate_auth"  # per-request memo on request.state: (Principal | None) or AuthError


def auth_error(status_code: int, code: str, message: str) -> HTTPException:
    """The auth layer's error shape: ``{"detail": {"code": ..., "message": ...}}``."""
    return AuthError(status_code, code, message).to_http()


def client_ip(conn: HTTPConnection) -> str | None:
    """The caller's address as uvicorn resolved it from trusted proxy headers (None if unknown).

    Fail closed for the internet: every request that came through Cloudflare carries
    ``CF-Connecting-IP``, so such a request is never treated as coming from home, even if
    FORWARDED_ALLOW_IPS is wrong and uvicorn reports the gateway's private address. Its real
    public address is used; a private-looking value (a spoof, or a test on the LAN) becomes an
    unparsable marker, which every home/private check treats as public."""
    cf = conn.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf if not is_private_address(cf) else f"cf:{cf}"
    return conn.client.host if conn.client else None


_SETUP_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")


def setup_host_ok(conn: HTTPConnection) -> bool:
    """First-run setup only on a name that belongs to the home network: an IP address,
    ``localhost``, a ``.local`` / ``.lan`` / ``.home`` / ``.home.arpa`` / ``.internal`` name,
    the public URL's host, or a name in CLIMATE_SETUP_HOSTS. Closes DNS rebinding: a web page
    on some other domain that rebinds to this server's LAN address would pass the private
    address and same-origin checks, but not this one."""
    raw = (conn.headers.get("x-forwarded-host") or conn.headers.get("host") or "").split(",")[0].strip().lower()
    host = raw[1:].split("]")[0] if raw.startswith("[") else raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    cfg = get_settings()
    allowed = {h.strip().lower() for h in cfg.setup_hosts.split(",") if h.strip()}
    if cfg.public_url:
        allowed.add((urlsplit(cfg.public_url).hostname or "").lower())
    return host == "localhost" or host.endswith(_SETUP_SUFFIXES) or host in allowed


def bearer_token(conn: HTTPConnection) -> str | None:
    """The bearer credential, or None when the request carries no Authorization header.

    A header that is present but is not ``Bearer <something>`` is an error (401), not
    "anonymous", so a client that mangles its header learns about it instead of silently
    falling back to public routes."""
    raw = conn.headers.get("authorization")
    if raw is None:
        return None
    scheme, _, value = raw.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "NOT_AUTHENTICATED", "Use an 'Authorization: Bearer <token>' header.")
    return value


def resolve_caller(conn: HTTPConnection) -> Principal | None:
    """The authenticated caller, or None for an anonymous request (no Authorization header).

    Raises ``AuthError`` for a credential that is present but bad (unknown, expired, revoked,
    refused from this address). The outcome is memoised on ``request.state`` so several
    dependencies on one request cost one database check."""
    memo = getattr(conn.state, _STATE_ATTR, None)
    if memo is not None:
        if isinstance(memo, AuthError):
            raise memo
        return memo[0]
    try:
        token = bearer_token(conn)
        principal = None if token is None else auth_service.resolve_bearer(token, ip=client_ip(conn))
    except AuthError as err:
        setattr(conn.state, _STATE_ATTR, err)
        raise
    setattr(conn.state, _STATE_ATTR, (principal,))
    return principal


def optional_caller(conn: HTTPConnection) -> Principal | None:
    """Like ``resolve_caller`` but a bad credential counts as anonymous (public routes such as
    ``/auth/state`` and ``/auth/logout`` must keep working with a stale token)."""
    try:
        return resolve_caller(conn)
    except AuthError:
        return None


def current_caller(request: Request) -> Principal:
    """Any authenticated caller; 401 NOT_AUTHENTICATED when there is no credential."""
    try:
        principal = resolve_caller(request)
    except AuthError as err:
        raise err.to_http() from None
    if principal is None or principal.role not in ALL_ROLES:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "NOT_AUTHENTICATED", "Sign in required.")
    return principal


def _admit(principal: Principal, roles: Iterable[str], message: str) -> Principal:
    if principal.role not in roles:
        raise auth_error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", message)
    return principal


def require_reader(principal: Principal = Depends(current_caller)) -> Role:
    """Reads: the owner and every API token role."""
    return cast(Role, _admit(principal, ALL_ROLES, "Not allowed.").role)


def require_control(principal: Principal = Depends(current_caller)) -> Role:
    """Everyday controls: the owner or a ``control`` token (never the agent or a viewer)."""
    return cast(Role, _admit(principal, ("owner", "control"), "Needs the owner or a control token.").role)


def require_owner(principal: Principal = Depends(current_caller)) -> Role:
    """Settings, mode, setup, hand-back, decisions, tokens, sessions, audit: the owner only."""
    return cast(Role, _admit(principal, ("owner",), "Owner only.").role)


def require_writer(principal: Principal = Depends(current_caller)) -> Role:
    """The gated writes both the owner and the agent may make (propose, sign off, reports)."""
    return cast(Role, _admit(principal, ("owner", "agent"), "Needs the owner or an agent token.").role)


def require_agent(principal: Principal = Depends(current_caller)) -> Role:
    """The agent service's own run bookkeeping (claim, heartbeat, finish)."""
    return cast(Role, _admit(principal, ("agent",), "Agent service only.").role)


def owner_caller(principal: Principal = Depends(current_caller)) -> Principal:
    """The owner, with the session id and device name (for routes that act on the session)."""
    return _admit(principal, ("owner",), "Owner only.")


def recent_owner(principal: Principal = Depends(owner_caller)) -> Principal:
    """The owner who re-entered the password within ``reauth_window_minutes`` (403
    REAUTHENTICATION_REQUIRED otherwise; the web asks for the password and replays)."""
    if not principal.recently_authenticated:
        raise auth_error(status.HTTP_403_FORBIDDEN, "REAUTHENTICATION_REQUIRED", "Enter your password again to continue.")
    return principal


CallerDep = Depends(current_caller)
ReaderDep = Depends(require_reader)
ControlDep = Depends(require_control)
OwnerDep = Depends(require_owner)
WriterDep = Depends(require_writer)
AgentDep = Depends(require_agent)
OwnerCallerDep = Depends(owner_caller)
RecentAuthDep = Depends(recent_owner)

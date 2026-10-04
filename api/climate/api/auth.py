"""Owner sign-in (one password, long-lived cookie) and bearer tokens for services.

Roles:
- ``owner``: everything. Signed-in browser or the iOS WKWebView wrapper (cookie).
- ``agent``: the Claude agent service and the MCP server (``Authorization: Bearer``).
  Read everything except setup secrets; may propose changes/experiments, sign off or hold
  model-proposed changes inside the pre-approved ranges, publish reports, request refits,
  backtests and simulations, and claim/finish its own runs. It can never reach a thermostat
  write, the controller mode, settings or setup.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from climate.config import get_settings
from climate.store.app_settings import OwnerSettings, get_setting, put_setting
from climate.store.db import session_scope

log = logging.getLogger(__name__)
Role = Literal["owner", "agent"]
COOKIE = "climate_session"
MAX_AGE_S = 180 * 24 * 3600
_SCRYPT = (2**15, 8, 1)  # n, r, p
_FAILS: dict[str, list[float]] = defaultdict(list)
_fallback_secret = secrets.token_urlsafe(32)


# --- password hashing (stdlib scrypt) --------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = _SCRYPT
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=2**26, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), maxmem=2**26, dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def password_set() -> bool:
    if get_settings().owner_password:
        return True
    with session_scope() as s:
        return get_setting(s, "owner", OwnerSettings).password_hash is not None


def check_owner_password(password: str) -> bool:
    env_pw = get_settings().owner_password
    if env_pw:
        return hmac.compare_digest(password.encode(), env_pw.encode())
    with session_scope() as s:
        h = get_setting(s, "owner", OwnerSettings).password_hash
    return bool(h) and verify_password(password, h)


def set_owner_password(password: str) -> None:
    with session_scope() as s:
        put_setting(s, "owner", OwnerSettings(password_hash=hash_password(password)), updated_by="owner")


# --- login throttling -------------------------------------------------------------------


def throttle(ip: str) -> None:
    now = time.monotonic()
    recent = [t for t in _FAILS[ip] if now - t < 300]
    _FAILS[ip] = recent
    if len(recent) >= 5:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; wait a few minutes.")


def record_failure(ip: str) -> None:
    _FAILS[ip].append(time.monotonic())


# --- cookies ------------------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    cfg = get_settings()
    secret = cfg.session_secret or cfg.secret_key
    if not secret:
        log.warning("CLIMATE_SESSION_SECRET not set; sign-ins reset when the API restarts.")
        secret = _fallback_secret
    return URLSafeTimedSerializer(secret, salt="climate-session")


def issue_cookie(response: Response) -> None:
    token = _serializer().dumps({"r": "owner"})
    response.set_cookie(
        COOKIE, token, max_age=MAX_AGE_S, httponly=True, samesite="lax", secure=get_settings().cookie_secure, path="/"
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


# --- role resolution ------------------------------------------------------------------


def role_from_request(request: Request) -> Role | None:
    cfg = get_settings()
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        for configured in (cfg.agent_token, cfg.mcp_token):
            if configured and hmac.compare_digest(token.encode(), configured.encode()):
                return "agent"
        return None
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=MAX_AGE_S)
    except BadSignature:
        return None
    return "owner" if data.get("r") == "owner" else None


def _same_origin(request: Request) -> bool:
    """Cookie-authenticated writes must come from our own origin (CSRF guard)."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # same-origin fetches from WKWebView may omit it; JSON bodies need CORS anyway
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


def get_role(request: Request) -> Role | None:
    return role_from_request(request)


def require_reader(request: Request) -> Role:
    role = role_from_request(request)
    if role is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.")
    return role


def require_owner(request: Request) -> Role:
    role = role_from_request(request)
    if role is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.")
    if role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner only.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request refused.")
    return role


def require_writer(request: Request) -> Role:
    """Owner or agent, for the gated endpoints both may call (propose, sign off, reports)."""
    role = require_reader(request)
    if role == "owner" and request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request refused.")
    return role


def require_agent(request: Request) -> Role:
    role = require_reader(request)
    if role != "agent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent service only.")
    return role


OwnerDep = Depends(require_owner)
ReaderDep = Depends(require_reader)
WriterDep = Depends(require_writer)
AgentDep = Depends(require_agent)

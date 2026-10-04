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
import threading
import time
from collections import defaultdict
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import text

from climate.config import get_settings
from climate.store.app_settings import OwnerSettings, get_raw, get_setting, put_setting
from climate.store.db import session_scope

log = logging.getLogger(__name__)
Role = Literal["owner", "agent"]
COOKIE = "climate_session"
MAX_AGE_S = 180 * 24 * 3600
_SCRYPT = (2**15, 8, 1)  # n, r, p (about 32 MiB per check)
_WINDOW_S = 300
_PER_IP_LIMIT = 5  # attempts per client per window
_GLOBAL_LIMIT = 30  # attempts across all clients per window (spoofed X-Forwarded-For can't dodge it)
_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_ALL_ATTEMPTS: list[float] = []
_LOCK = threading.Lock()
_CHECKS = threading.BoundedSemaphore(2)  # at most two scrypt checks at once
_fallback_secret = secrets.token_urlsafe(32)
_VERSION_CACHE: dict[str, tuple[float, str]] = {}


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
    """Set or change the password. Signs out every existing session (new session epoch)."""
    with session_scope() as s:
        current = get_setting(s, "owner", OwnerSettings)
        put_setting(
            s, "owner",
            OwnerSettings(password_hash=hash_password(password), session_epoch=current.session_epoch + 1),
            updated_by="owner",
        )
    _VERSION_CACHE.clear()


def claim_first_password(password: str) -> bool:
    """First run: set the password only if none is set, atomically. False if someone else won."""
    if get_settings().owner_password:
        return False
    encoded = hash_password(password)
    with session_scope() as s:
        get_setting(s, "owner", OwnerSettings)  # make sure the seed row exists
        if get_raw(s, "owner") is None:
            put_setting(s, "owner", OwnerSettings(), updated_by="seed")
            s.flush()
        won = s.execute(
            text(
                "UPDATE app_settings SET value = jsonb_set(value, '{password_hash}', to_jsonb(CAST(:h AS text))),"
                " updated_at = now(), updated_by = 'owner'"
                " WHERE key = 'owner' AND (value->>'password_hash') IS NULL RETURNING key"
            ),
            {"h": encoded},
        ).first()
    _VERSION_CACHE.clear()
    return won is not None


def sign_out_everywhere() -> None:
    with session_scope() as s:
        current = get_setting(s, "owner", OwnerSettings)
        current.session_epoch += 1
        put_setting(s, "owner", current, updated_by="owner")
    _VERSION_CACHE.clear()


def _session_version() -> str:
    """Changes when the password changes or the owner signs out everywhere; old cookies die."""
    cached = _VERSION_CACHE.get("v")
    if cached and time.monotonic() - cached[0] < 5:
        return cached[1]
    cfg = get_settings()
    with session_scope() as s:
        owner = get_setting(s, "owner", OwnerSettings)
    secret_material = cfg.owner_password or owner.password_hash or ""
    fp = hashlib.sha256(f"{secret_material}|{owner.session_epoch}".encode()).hexdigest()[:16]
    _VERSION_CACHE["v"] = (time.monotonic(), fp)
    return fp


# --- login throttling -------------------------------------------------------------------


def _prune(now: float) -> None:
    cutoff = now - _WINDOW_S
    for ip in list(_ATTEMPTS):
        _ATTEMPTS[ip] = [t for t in _ATTEMPTS[ip] if t > cutoff]
        if not _ATTEMPTS[ip]:
            del _ATTEMPTS[ip]
    _ALL_ATTEMPTS[:] = [t for t in _ALL_ATTEMPTS if t > cutoff]


def begin_attempt(ip: str) -> float:
    """Reserve a login attempt BEFORE checking the password (parallel requests can't slip
    through). Raises 429 past the per-client or global limit. Returns a token for forgive()."""
    with _LOCK:
        now = time.monotonic()
        _prune(now)
        if len(_ATTEMPTS.get(ip, [])) >= _PER_IP_LIMIT or len(_ALL_ATTEMPTS) >= _GLOBAL_LIMIT:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; wait a few minutes.")
        _ATTEMPTS[ip].append(now)
        _ALL_ATTEMPTS.append(now)
        return now


def forgive(ip: str, token: float) -> None:
    """A successful sign-in doesn't count against the limits."""
    with _LOCK:
        for bucket in (_ATTEMPTS.get(ip), _ALL_ATTEMPTS):
            if bucket and token in bucket:
                bucket.remove(token)


def checked_password(password: str) -> bool:
    """check_owner_password with at most two scrypt computations running at once."""
    with _CHECKS:
        return check_owner_password(password)


# --- cookies ------------------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().session_secret
    if not secret:
        # Deliberately NOT derived from CLIMATE_SECRET_KEY: rotating the session secret must
        # never be tied to the key that decrypts the stored tokens.
        log.warning("CLIMATE_SESSION_SECRET not set; sign-ins reset when the API restarts.")
        secret = _fallback_secret
    return URLSafeTimedSerializer(secret, salt="climate-session")


def issue_cookie(response: Response) -> None:
    token = _serializer().dumps({"r": "owner", "v": _session_version()})
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
    if data.get("r") != "owner" or not isinstance(data.get("v"), str):
        return None
    return "owner" if hmac.compare_digest(data["v"], _session_version()) else None


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

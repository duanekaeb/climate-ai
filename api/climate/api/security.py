"""Security primitives for the owner login and API tokens (no database access here).

Spec: docs/specs/users-and-tokens.md. The service layer (``climate.auth_service``) builds on
these; nothing here touches the database or the request.

- Passwords: Argon2id through ``argon2-cffi`` ``PasswordHasher()`` (library defaults: t=3,
  m=64 MiB, p=4). Hashes from before the switch (``scrypt$n$r$p$salt$digest``) still verify,
  and ``password_needs_rehash`` says so, so the next successful sign-in upgrades them.
  ``dummy_verify`` spends the same Argon2 cost when no password is set (constant timing).
- Tokens: one ``hash_token`` (HMAC-SHA256 keyed with ``CLIMATE_TOKEN_PEPPER``, hex) for refresh
  tokens and API tokens; the raw value is never stored. ``new_secret`` is 32 random bytes,
  URL-safe.
- Access tokens: HS256 JWTs signed with ``CLIMATE_JWT_SECRET``; claims ``sub="owner"``,
  ``role="owner"``, ``sid`` (session id), ``iat``, ``exp``, ``jti``, ``typ="access"``,
  ``iss`` / ``aud`` ``"climate-ai"``. ``decode_access_token`` raises ``AuthError``
  TOKEN_EXPIRED (refresh and replay) or NOT_AUTHENTICATED (anything else wrong).
- ``is_private_address``: LAN, Docker, Tailscale and loopback (IPv4 and IPv6, including
  IPv4-mapped IPv6). Callers pass the client address AFTER trusted proxy headers (uvicorn
  ``--proxy-headers`` with ``--forwarded-allow-ips``), never a raw X-Forwarded-For.
- ``config_problem``: the JWT secret and the pepper must be set (at least 32 characters each);
  otherwise sign-in and token checks answer 503 AUTH_NOT_CONFIGURED.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException

from climate.config import get_settings
from climate.timeutil import utcnow

ISSUER = "climate-ai"
AUDIENCE = "climate-ai"
JWT_ALGORITHM = "HS256"
MIN_SECRET_CHARS = 32
_JWT_LEEWAY_S = 5
_SCRYPT_MAXMEM = 2**26

# The spec's private ranges: RFC 1918, loopback, CGNAT (Tailscale), IPv6 loopback, ULA and
# link-local. Docker bridge networks fall inside 172.16/12 (or 192.168/16, 10/8).
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "100.64.0.0/10",
        "::1/128", "fc00::/7", "fe80::/10",
    )
)

_hasher = PasswordHasher()


class AuthError(Exception):
    """An auth failure the API layer turns into ``HTTPException(status, {"code", "message"})``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    def to_http(self) -> HTTPException:
        headers = {"WWW-Authenticate": "Bearer"} if self.status == 401 else None
        return HTTPException(self.status, detail={"code": self.code, "message": self.message}, headers=headers)


# --- configuration ------------------------------------------------------------------------


def config_problem() -> str | None:
    """Why tokens cannot be issued or checked, or None when the secrets are in place."""
    cfg = get_settings()
    missing = [name for name, value in (("CLIMATE_JWT_SECRET", cfg.jwt_secret), ("CLIMATE_TOKEN_PEPPER", cfg.token_pepper))
               if len(value) < MIN_SECRET_CHARS]
    if missing:
        return (f"{' and '.join(missing)} must be set to at least {MIN_SECRET_CHARS} random characters "
                "(python -m climate.cli gen-key prints new ones)")
    return None


def require_configured() -> None:
    """Raise 503 AUTH_NOT_CONFIGURED when the JWT secret or the pepper is missing."""
    problem = config_problem()
    if problem:
        raise AuthError(503, "AUTH_NOT_CONFIGURED", f"Sign-in is not configured: {problem}.")


# --- passwords ----------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Argon2id hash (salted, self-describing) of a new password."""
    return _hasher.hash(password)


def _verify_scrypt(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                                maxmem=_SCRYPT_MAXMEM, dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str | None) -> bool:
    """True when ``password`` matches ``encoded`` (Argon2id, or a legacy scrypt hash).

    With no stored hash, still spends one Argon2 verification so timing does not tell an
    attacker whether a password is set."""
    if not encoded:
        dummy_verify(password)
        return False
    if encoded.startswith("scrypt$"):
        return _verify_scrypt(password, encoded)
    try:
        return _hasher.verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    """True for legacy scrypt hashes and Argon2 hashes made with weaker parameters."""
    if encoded.startswith("scrypt$"):
        return True
    try:
        return _hasher.check_needs_rehash(encoded)
    except (InvalidHashError, ValueError):
        return True


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def dummy_verify(password: str) -> None:
    """Spend one Argon2 verification against a throwaway hash (constant-time refusals)."""
    try:
        _hasher.verify(_dummy_hash(), password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


# --- opaque tokens ------------------------------------------------------------------------


def new_secret() -> str:
    """32 random bytes, URL-safe base64 (43 characters; may contain '-' and '_')."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """HMAC-SHA256 of a refresh or API token with the pepper (hex). Requires the pepper."""
    pepper = get_settings().token_pepper
    if not pepper:
        raise AuthError(503, "AUTH_NOT_CONFIGURED", "Sign-in is not configured: CLIMATE_TOKEN_PEPPER is not set.")
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- access tokens (JWT) ------------------------------------------------------------------


def encode_access_token(session_id: int, now: datetime | None = None) -> tuple[str, int]:
    """A signed access token for the owner's session ``session_id``; returns (token, ttl s)."""
    require_configured()
    cfg = get_settings()
    now = now or utcnow()
    ttl = cfg.access_ttl_minutes * 60
    claims = {
        "sub": "owner",
        "role": "owner",
        "sid": session_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": "access",
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    return jwt.encode(claims, cfg.jwt_secret, algorithm=JWT_ALGORITHM), ttl


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify signature, expiry, issuer, audience and type; return the claims.

    Raises AuthError 401 TOKEN_EXPIRED for an expired but otherwise valid token, and 401
    NOT_AUTHENTICATED for anything else (bad signature, wrong claims, not a JWT)."""
    require_configured()
    try:
        claims = jwt.decode(
            token, get_settings().jwt_secret, algorithms=[JWT_ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
            leeway=_JWT_LEEWAY_S, options={"require": ["exp", "iat", "sub", "sid", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(401, "TOKEN_EXPIRED", "The access token has expired; refresh it.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.") from exc
    if claims.get("typ") != "access" or claims.get("sub") != "owner" or claims.get("role") != "owner" \
            or not isinstance(claims.get("sid"), int):
        raise AuthError(401, "NOT_AUTHENTICATED", "Sign in required.")
    return claims


# --- addresses ----------------------------------------------------------------------------


def is_private_address(ip: str | None) -> bool:
    """True for LAN, Docker, Tailscale and loopback addresses (the spec's ranges).

    Unknown or unparsable addresses are treated as public (the safe side)."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip().split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _PRIVATE_NETS if net.version == addr.version)

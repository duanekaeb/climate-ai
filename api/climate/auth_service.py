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
  PASSWORD_NOT_SET, WEAK_PASSWORD, FORBIDDEN, AUTH_NOT_CONFIGURED, NOT_FOUND, INVALID_REQUEST.
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
- ``resolve_bearer(token: str, *, ip: str | None) -> Principal``: a ``cai_`` API token, a legacy
  env token (CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN, role agent, private addresses only) or an
  access JWT (re-checks the session row).
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

# Spec: user accounts, sessions and API tokens (GrowWise pattern)

Replaces the single owner password and the env-only bearer tokens. Modelled on GrowWise AI's
auth (`growwise-ai/apps/api/app/core/security.py`, `core/deps.py`, `modules/auth/*`), minus its
family/kid domain. Contract already in code: migration `004_users_and_tokens.sql`, ORM `User`,
`AuthSession`, `UserInvitation`, `PasswordResetToken`, `ApiToken`, `AuditEvent`,
`ControlAction.requested_by_user_id`, settings in `config.py` (`jwt_secret`, `token_pepper`,
`access_ttl_minutes`, `refresh_ttl_days`, `session_max_days`, `reauth_window_minutes`,
`password_min_length`, `login_max_attempts`, `login_lockout_minutes`, `allow_remote_setup`), and
the wire types in `api/schemas.py` (Role/UserRole/TokenRole, UserOut ... AuthErrorDetail).

## Credentials

- **Passwords:** Argon2id via `argon2-cffi` `PasswordHasher()` (add the pinned dependency).
  Existing `scrypt$...` hashes still verify and are rehashed to Argon2id on the next successful
  sign-in. Minimum length `password_min_length` (10) for new passwords; the first-run and CLI
  paths may allow 8 only where the existing owner password predates the rule.
- **Access token:** JWT HS256 signed with `CLIMATE_JWT_SECRET`, 15 min, claims `sub` (user id),
  `role`, `sid` (session id), `iat`, `exp`, `jti`, `typ="access"`, `iss="climate-ai"`,
  `aud="climate-ai"`. Returned in JSON only; the web keeps it in memory. Every request re-reads
  the user and session from the database (deactivation / revocation take effect at once).
- **Refresh token:** `"<session id hex>.<secret>"` in cookie `climate_refresh`: HttpOnly,
  `Path=/api/auth`, `SameSite=Lax`, `Secure=CLIMATE_COOKIE_SECURE`, max-age = sliding expiry.
  Rotates on every `/auth/refresh`; presenting the previous (rotated-out) secret revokes the
  whole family (`family_id`) with reason `reuse_detected`. Sliding `refresh_ttl_days` (30),
  capped by `absolute_expires_at` = created + `session_max_days` (90).
- **API tokens:** `"cai_<id hex>_<secret>"` (`secret = token_urlsafe(32)`), stored as
  HMAC-SHA256(pepper) in `api_tokens.token_hash`, hint = last 4 chars. Shown once. Roles
  `agent` (read + the gated agent endpoints), `viewer` (read), `member` (read + everyday
  controls). Never admin. `local_only` (default true): refused unless the client address
  (after the trusted proxy headers) is private: 10/8, 172.16/12, 192.168/16, 127/8, 100.64/10
  (Tailscale), ::1, fc00::/7, fe80::/10. Checked live: not revoked, not expired, creator still
  active (when it has one). `last_used_at`/`last_used_ip` written at most once a minute.
- **Legacy env tokens:** `CLIMATE_AGENT_TOKEN` / `CLIMATE_MCP_TOKEN` keep working as role
  `agent`, constant-time compared, **local_only** (private addresses only), logged once as
  deprecated. New installs get a `cai_` token for the agent from `scripts/bootstrap.sh` via
  `python -m climate.cli create-token --name agent --role agent --print-only-token` (the CLI
  prints the token once) — or keep the env token; both work.
- All token hashes (refresh, API, invitation, reset) use the same `hash_token()` HMAC with
  `CLIMATE_TOKEN_PEPPER`. With an empty `jwt_secret` or `token_pepper` (outside tests) every
  sign-in and token check refuses with a clear 503 "auth is not configured" and `doctor` fails.

## Roles and permissions

| Dependency (`climate.api.auth`) | Allowed | Used for |
|---|---|---|
| `ReaderDep` | admin, member, viewer, agent | every GET except users / tokens / audit / setup secrets |
| `MemberDep` (new) | admin, member, member-tokens | everyday controls: `POST /control/hold`, `/control/resume`, `/control/automatic`, `/control/presence`, `/utility-events/{id}/skip|unskip`, `/alerts/{id}/resolve`, `/agent/ask`, `/agent/run` |
| `OwnerDep` = `AdminDep` | admin (users only) | `/control/mode`, `PUT /control/settings`, `/control/handback`, `/setup/*`, change & experiment decisions that are owner-only today, `/admin/*`, `/auth/sessions` of others |
| `WriterDep` | admin, agent | the gated agent writes it covers today (propose, sign off / hold) |
| `AgentDep` | agent | `/agent/claim`, `/agent/heartbeat`, `/agent/runs/{id}/finish` |
| `RecentAuthDep` (new, on top of AdminDep) | admin who re-entered the password within `reauth_window_minutes` | creating/deleting users, changing roles, issuing reset links, creating API tokens |

The agent role can never reach a thermostat write, the controller mode, settings, setup, users
or tokens. A guard test walks every route in the app and asserts the matrix above for each role
(no route without an auth dependency except the public list: `/api/health`, `/api/auth/state`,
`/api/auth/setup`, `/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout`,
`/api/auth/invitation`, `/api/auth/accept-invitation`, `/api/auth/reset-password`, and the
static web app).

`control_actions.requested_by_user_id` is set on every owner/member action queued from the app;
`control_actions.actor` stays `'owner'` (= "a person through the app").

## Endpoints

Public (`/api/auth/*`):
- `GET /auth/state` → `AuthState` (`password_set` = any user exists; `setup_allowed` = no user
  and the client is private, or `allow_remote_setup`; `user`/`role` when a valid bearer is sent).
- `POST /auth/setup` (`SetupBody`) → `AccessTokenOut` + refresh cookie. Atomic "no users exist"
  claim (409 otherwise); 403 `SETUP_NOT_ALLOWED` from a public address unless
  `allow_remote_setup`. **Migration of the old owner password:** if `app_settings['owner']` has a
  `password_hash` (or `CLIMATE_OWNER_PASSWORD` is set) and no user exists, the app creates admin
  user `owner` with that hash at startup (idempotent), so the existing password keeps working
  with username `owner`; the UI says so on the login page when only that user exists.
- `POST /auth/login` (`LoginBody`) → `AccessTokenOut` + cookie. Generic 401
  `INVALID_CREDENTIALS`; per-account lockout `login_max_attempts` failures in
  `login_lockout_minutes` (decaying: the count resets after a quiet lockout window) → 423
  `ACCOUNT_LOCKED`; plus a per-IP limit (10 / 5 min) in memory. No global cap (it let anyone
  lock the owner out). Always one hash check, even for unknown users (timing).
- `POST /auth/refresh` (cookie) → `AccessTokenOut` + rotated cookie; 401 `SESSION_REVOKED` when
  revoked/expired/reused.
- `POST /auth/logout` (cookie; optional bearer) → 204; revokes that session, clears cookie.
- `GET /auth/invitation?token=` → `InvitationInfo`; `POST /auth/accept-invitation` →
  `AccessTokenOut` + cookie.
- `POST /auth/reset-password` (`ResetPasswordBody`) → 204; revokes all the user's sessions.
Bearer (any signed-in user unless noted):
- `GET /auth/me` → `MeOut`; `POST /auth/reauth` (`ReauthBody`, throttled like login) → `MeOut`;
  `POST /auth/change-password` (revokes the user's other sessions) → 204;
  `GET /auth/sessions` → `SessionOut[]` (own); `DELETE /auth/sessions/{id}` (own) → 204;
  `POST /auth/logout-all` → 204 (all own sessions incl. this one);
  `POST /auth/ws-ticket` → `WsTicketOut` (single use, 30 s, kept in memory; carries the
  principal; `/api/ws?ticket=` consumes it).
Admin (`AdminDep`; mutations also `RecentAuthDep` where the matrix says):
- `GET /admin/users` → `UserOut[]`; `POST /admin/users` (`UserCreateBody`) → `UserCreateOut`;
  `PATCH /admin/users/{id}` (`UserUpdateBody`) → `UserOut`; `POST /admin/users/{id}/reset-link`
  → `ResetLinkOut` (60 min); `POST /admin/users/{id}/unlock` → `UserOut`;
  `GET /admin/users/{id}/sessions` → `SessionOut[]`; `DELETE /admin/users/{id}/sessions` → 204.
- `GET /admin/invitations` → `InvitationOut[]`; `DELETE /admin/invitations/{id}` → 204
  (invitations last 14 days; URL = `{public base}/invite?token=…`, base from
  `CLIMATE_PUBLIC_URL` else the request's origin).
- `GET /admin/tokens` → `ApiTokenOut[]`; `POST /admin/tokens` (`ApiTokenCreateBody`) →
  `ApiTokenCreated`; `DELETE /admin/tokens/{id}` → 204 (revoke).
- `GET /admin/audit?event_type=&limit=` → `AuditEventOut[]`.
Rules: never remove, deactivate or demote the last active admin; an admin cannot deactivate or
demote themselves; deactivating a user revokes their sessions and the API tokens they created
(reactivation does not restore tokens). Every auth/admin action writes an `audit_events` row in
the same transaction (`auth.login`, `auth.login_failed`, `auth.locked`, `auth.logout`,
`auth.refresh_reuse`, `user.create`, `user.update`, `user.invite`, `user.reset_link`,
`user.unlock`, `token.create`, `token.revoke`, `session.revoke`, `password.change`,
`password.reset`, `setup.first_admin`); payloads never contain secrets.

Errors from the auth layer: `HTTPException(status, detail={"code": ..., "message": ...})`
(`AuthErrorDetail`). Other errors keep FastAPI's plain `{"detail": "..."}`.

WebSocket `/api/ws`: accepts `?ticket=` (single use) or an `Authorization: Bearer` header (API
tokens / agent). The old cookie path is removed. Origin check stays for ticket connections.

CSRF: the API is bearer-only; the only cookie (`climate_refresh`) is scoped to `/api/auth` and
used only by `refresh` / `logout`, which keep the existing same-origin (Origin /
X-Forwarded-Host) check.

## CLI (`python -m climate.cli`)

`create-admin --username U [--password-stdin]`, `add-user --username U --role R
[--password-stdin]`, `reset-password --username U [--password-stdin]` (break-glass; revokes
sessions), `unlock --username U`, `list-users`, `create-token --name N [--role agent|viewer|member]
[--expires-days D] [--allow-remote] [--print-only-token]`, `list-tokens`, `revoke-token --id N`.
They call the same service functions as the API, so audit rows match (actor `system`).
`set-password` (old) becomes an alias of `reset-password --username owner`.

## Web

`web/src/api/client.ts`: bearer from memory; `credentials: 'include'` for `/api/auth/*`;
single-flight refresh on 401 `TOKEN_EXPIRED` then one replay; 401 `SESSION_REVOKED` /
`NOT_AUTHENTICATED` → clear and go to `/login`; 403 `REAUTHENTICATION_REQUIRED` → a global
password dialog (`/auth/reauth`) then one replay. `bootstrapSession()` (refresh → me) runs
before the router guards. Guards: `requireAuth`, `requireRole('admin')`, redirect-if-signed-in.
Pages: Login (username + password; first-run banner when `password_set` is false and
`setup_allowed`), First-run setup, Accept invitation (`/invite?token=`), Reset password
(`/reset?token=`). Settings → "People & access" (admin): Users (list, create with temporary
password or invite link shown once with a copy button, role, deactivate/reactivate, reset link,
unlock, sessions), API tokens (create: name, role, expiry, local-only; one-time reveal + copy;
list with last used; revoke), Audit log. Every user: Account (display name, change password,
my sessions with revoke, sign out everywhere). UI gating: member sees everyday controls; viewer
sees no write buttons; admin sees everything. WebSocket uses a ticket. Light/dark, 375 px.

## Ownership (parallel builders)

| Builder | Files |
|---|---|
| auth-core | new `api/climate/api/security.py`, new `api/climate/auth_service.py` (users, sessions, tokens, invitations, resets, lockout, audit, owner migration), `api/climate/cli.py` (auth commands), `api/pyproject.toml` (argon2-cffi, PyJWT), tests `api/tests/test_auth_service.py`, `test_cli_users.py` |
| auth-api | `api/climate/api/auth.py` (resolver + deps), `api/climate/api/routers/auth.py`, new `routers/admin.py`, `routers/ws.py`, dependency changes in every `api/climate/api/routers/*.py`, `api/climate/api/app.py`, `api/tests/conftest.py` (fixtures), `test_api_auth.py`, `test_api_roles.py`, new `test_api_admin.py`, `test_route_guard.py`, other `test_api_*.py` fixture updates |
| web | `web/src/**` (except generated `api/types.ts`/`schema.json`) |
| agent-ios | `agent/**`, `ios/**` |
| local-run | `docker-compose.yml`, `.env.example`, `docker/*`, `scripts/*`, `Makefile`, `README.md`, `docs/DEPLOY.md`, new `docs/LOCAL_DEVELOPMENT.md`, `api/climate/cli.py` `doctor` only (coordinate: auth-core owns the rest of cli.py) |
| homekit | `sources/homekit.py`, `collector/homekit_service.py`, `docs/HOMEKIT.md`, homekit tests |
| gateway | `/home/user/growwise-ai/apprelay/**` (PR), Climate's `docker-compose.nginx.yml`, `deploy/nginx/*`, new `docs/PUBLIC_ACCESS.md` |

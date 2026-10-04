"""Owner sign-in and signed-in devices. Spec: docs/specs/users-and-tokens.md.

One password, no user accounts. Signing in (``/auth/setup`` on first run, ``/auth/login``)
returns a 15-minute access token as JSON (the web app keeps it in memory and sends it as
``Authorization: Bearer``) and sets the refresh token as an HttpOnly cookie scoped to
``/api/auth``. ``/auth/refresh`` rotates that cookie and mints a new access token; presenting
an old refresh token again revokes the device (token theft). The cookie is the only ambient
credential, so the two routes that act on it alone (refresh, logout) refuse cross-origin
requests; everything else needs the bearer header, which another site cannot make a browser send.

Public: state, setup (private addresses only unless ``allow_remote_setup``), login, refresh,
logout. Any caller: me, ws-ticket. Owner: reauth, change-password, sessions, logout-all.
The service layer (``climate.auth_service``) does the work and writes the audit rows; this
module maps HTTP onto it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi import status as http
from fastapi.responses import JSONResponse

from climate import auth_service
from climate.api import auth
from climate.api.auth import CallerDep, OwnerCallerDep, Principal
from climate.api.schemas import (
    AccessTokenOut,
    AuthState,
    ChangePasswordBody,
    LoginBody,
    MeOut,
    ReauthBody,
    SessionOut,
    SetupBody,
    WsTicketOut,
)
from climate.api.security import AuthError
from climate.config import get_settings

router = APIRouter(tags=["auth"])
_UA_MAX = 400


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:_UA_MAX]


def same_origin(request: Request) -> bool:
    """Cookie-only requests must come from our own origin (CSRF guard).

    No ``Origin`` header passes: same-origin fetches from the iOS WKWebView may omit it, and a
    cross-site form post cannot reach these routes with a JSON body anyway. ``Origin: null``
    (sandboxed frames, file pages) never matches."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


def _require_same_origin(request: Request) -> None:
    if not same_origin(request):
        raise AuthError(http.HTTP_403_FORBIDDEN, "FORBIDDEN", "Cross-origin request refused.")


def set_refresh_cookie(response: Response, issued: auth_service.Issued) -> None:
    """HttpOnly, Path=/api/auth, SameSite=Lax, Secure per CLIMATE_COOKIE_SECURE."""
    response.set_cookie(
        auth_service.REFRESH_COOKIE,
        issued.refresh_token,
        max_age=issued.refresh_max_age,
        path=auth_service.REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        auth_service.REFRESH_COOKIE,
        path=auth_service.REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )


def _token_out(issued: auth_service.Issued) -> AccessTokenOut:
    return AccessTokenOut(access_token=issued.access_token, expires_in=issued.expires_in, session_id=issued.session_id)


def _me(principal: Principal) -> MeOut:
    is_token = principal.kind in ("api_token", "env_token")
    return MeOut(
        role=principal.role,
        session_id=principal.session_id if principal.kind == "session" else None,
        token_id=principal.token_id if principal.kind == "api_token" else None,
        token_name=principal.label if is_token else None,
        recently_authenticated=principal.kind == "session" and principal.recently_authenticated,
    )


def _error_response(err: AuthError, *, clear_cookie: bool) -> JSONResponse:
    """An AuthError as a response that can still carry Set-Cookie (an exception would drop it)."""
    headers = {"WWW-Authenticate": "Bearer"} if err.status == http.HTTP_401_UNAUTHORIZED else None
    resp = JSONResponse({"detail": {"code": err.code, "message": err.message}}, status_code=err.status, headers=headers)
    if clear_cookie:
        clear_refresh_cookie(resp)
    return resp


# --- public -------------------------------------------------------------------------------


@router.get("/auth/state", response_model=AuthState)
def auth_state(request: Request) -> AuthState:
    """Public. ``authenticated`` reflects the bearer on this request (the web app refreshes
    first, so a stale or missing token reads as signed out rather than failing)."""
    principal = auth.optional_caller(request)
    return AuthState(
        authenticated=principal is not None,
        role=principal.role if principal is not None else None,
        password_set=auth_service.password_set(),
        setup_allowed=auth_service.setup_allowed(auth.client_ip(request)),
    )


@router.post("/auth/setup", response_model=AccessTokenOut)
def setup(body: SetupBody, request: Request, response: Response) -> AccessTokenOut:
    """First run only: choose the owner password and sign this device in. Accepted only from
    a private address (home network, Docker, Tailscale) unless ``allow_remote_setup``: 403
    SETUP_NOT_ALLOWED, 409 ALREADY_SET, 422 WEAK_PASSWORD."""
    issued = auth_service.setup(
        body.password, ip=auth.client_ip(request), user_agent=_user_agent(request), device_name=body.device_name
    )
    set_refresh_cookie(response, issued)
    return _token_out(issued)


@router.post("/auth/login", response_model=AccessTokenOut)
def login(body: LoginBody, request: Request, response: Response) -> AccessTokenOut:
    """Sign in with the password: 401 INVALID_CREDENTIALS, 429 TOO_MANY_ATTEMPTS (this address)
    or LOGIN_PAUSED (internet sign-in paused after many failures; home still works)."""
    issued = auth_service.login(
        body.password, ip=auth.client_ip(request), user_agent=_user_agent(request), device_name=body.device_name
    )
    set_refresh_cookie(response, issued)
    return _token_out(issued)


@router.post("/auth/refresh", response_model=AccessTokenOut)
def refresh(request: Request, response: Response):
    """Trade the refresh cookie for a new access token and a rotated cookie. A missing,
    unknown, expired or reused cookie is 401 and the cookie is cleared."""
    _require_same_origin(request)
    try:
        issued = auth_service.refresh(
            request.cookies.get(auth_service.REFRESH_COOKIE), ip=auth.client_ip(request), user_agent=_user_agent(request)
        )
    except AuthError as err:
        return _error_response(err, clear_cookie=err.status == http.HTTP_401_UNAUTHORIZED)
    set_refresh_cookie(response, issued)
    return _token_out(issued)


@router.post("/auth/logout", status_code=http.HTTP_204_NO_CONTENT, response_class=Response)
def logout(request: Request) -> Response:
    """Sign this device out: revoke the session named by the refresh cookie or the bearer
    (an API token is not a session and is left alone) and clear the cookie. Always 204."""
    _require_same_origin(request)
    principal = auth.optional_caller(request)
    auth_service.logout(
        request.cookies.get(auth_service.REFRESH_COOKIE),
        principal=principal if principal is not None and principal.kind == "session" else None,
        ip=auth.client_ip(request),
    )
    resp = Response(status_code=http.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(resp)
    return resp


# --- any signed-in caller -----------------------------------------------------------------


@router.get("/auth/me", response_model=MeOut)
def me(principal: Principal = CallerDep) -> MeOut:
    return _me(principal)


@router.post("/auth/ws-ticket", response_model=WsTicketOut)
def ws_ticket(principal: Principal = CallerDep) -> WsTicketOut:
    """A single-use, 30-second ticket for ``/api/ws?ticket=…`` carrying the caller's role
    (browsers cannot put a bearer header on a WebSocket)."""
    ticket, expires_in = auth_service.issue_ws_ticket(principal)
    return WsTicketOut(ticket=ticket, expires_in=expires_in)


# --- owner --------------------------------------------------------------------------------


@router.post("/auth/reauth", response_model=MeOut)
def reauth(body: ReauthBody, request: Request, principal: Principal = OwnerCallerDep) -> MeOut:
    """Re-enter the password to open the recent-auth window (needed to create API tokens)."""
    return _me(auth_service.reauth(principal, body.password, ip=auth.client_ip(request)))


@router.post("/auth/change-password", status_code=http.HTTP_204_NO_CONTENT, response_class=Response)
def change_password(body: ChangePasswordBody, request: Request, principal: Principal = OwnerCallerDep) -> Response:
    """Needs the current password; signs out every other device."""
    auth_service.change_password(principal, body.current_password, body.new_password, ip=auth.client_ip(request))
    return Response(status_code=http.HTTP_204_NO_CONTENT)


@router.get("/auth/sessions", response_model=list[SessionOut])
def list_sessions(principal: Principal = OwnerCallerDep) -> list[SessionOut]:
    """Signed-in devices, newest activity first; ``current`` marks this one."""
    return [
        SessionOut(
            id=s.id,
            device_name=s.device_name,
            user_agent=s.user_agent,
            ip=s.ip,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
            current=s.id == principal.session_id,
        )
        for s in auth_service.list_sessions()
    ]


@router.delete("/auth/sessions/{session_id}", status_code=http.HTTP_204_NO_CONTENT, response_class=Response)
def revoke_session(session_id: int, request: Request, principal: Principal = OwnerCallerDep) -> Response:
    """Sign one device out (404 when there is no such open session)."""
    if not auth_service.revoke_session(principal, session_id, ip=auth.client_ip(request)):
        raise auth.auth_error(http.HTTP_404_NOT_FOUND, "NOT_FOUND", "No such signed-in device.")
    return Response(status_code=http.HTTP_204_NO_CONTENT)


@router.post("/auth/logout-all", status_code=http.HTTP_204_NO_CONTENT, response_class=Response)
def logout_all(request: Request, principal: Principal = OwnerCallerDep) -> Response:
    """Sign out every device, this one included, and clear this device's cookie."""
    auth_service.logout_all(principal, ip=auth.client_ip(request))
    resp = Response(status_code=http.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(resp)
    return resp

"""Owner sign-in: first-run password setup, login (throttled per client IP), logout.

The password hash lives in app_settings['owner'] (or CLIMATE_OWNER_PASSWORD); the session is
a signed, http-only cookie. Bearer tokens (agent / MCP) never touch these routes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import status as http

from climate.api import auth
from climate.api.schemas import AuthState, LoginBody, PasswordBody

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    # uvicorn resolves X-Forwarded-For from trusted proxies (--proxy-headers); trusting the
    # header here directly would let anyone dodge the throttle by changing it.
    return request.client.host if request.client else "unknown"


@router.get("/auth/state", response_model=AuthState)
def auth_state(request: Request) -> AuthState:
    role = auth.role_from_request(request)
    return AuthState(authenticated=role is not None, role=role, password_set=auth.password_set())


@router.post("/auth/setup", response_model=AuthState)
def setup_password(body: PasswordBody, response: Response) -> AuthState:
    """First run only: choose the owner password and sign in."""
    if auth.password_set() or not auth.claim_first_password(body.password):
        raise HTTPException(http.HTTP_409_CONFLICT, "A password is already set; sign in instead.")
    auth.issue_cookie(response)
    return AuthState(authenticated=True, role="owner", password_set=True)


@router.post("/auth/login", response_model=AuthState)
def login(body: LoginBody, request: Request, response: Response) -> AuthState:
    ip = _client_ip(request)
    if not auth.password_set():
        raise HTTPException(http.HTTP_409_CONFLICT, "No password is set yet; choose one first.")
    attempt = auth.begin_attempt(ip)
    if not auth.checked_password(body.password):
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Wrong password.")
    auth.forgive(ip, attempt)
    auth.issue_cookie(response)
    return AuthState(authenticated=True, role="owner", password_set=True)


@router.post("/auth/logout-everywhere", response_model=AuthState)
def logout_everywhere(response: Response, _: auth.Role = auth.OwnerDep) -> AuthState:
    """Invalidate every owner session (all browsers and the iOS app), including this one."""
    auth.sign_out_everywhere()
    auth.clear_cookie(response)
    return AuthState(authenticated=False, role=None, password_set=auth.password_set())


@router.post("/auth/logout", response_model=AuthState)
def logout(request: Request, response: Response) -> AuthState:
    auth.clear_cookie(response)
    # A bearer caller stays authenticated (its token is not a session); a cookie is gone.
    role = auth.role_from_request(request)
    still = role if role == "agent" else None
    return AuthState(authenticated=still is not None, role=still, password_set=auth.password_set())

"""API tokens and the audit trail (owner only). Spec: docs/specs/users-and-tokens.md.

API tokens are how services and scripts call Climate AI: the Claude agent and the MCP server
(role ``agent``), a dashboard (``viewer``), a wall panel or a shortcut that sets holds
(``control``). None of them gets the owner's full rights. A token is ``cai_<id hex>_<secret>``,
shown exactly once in the create response and stored only as an HMAC; listing shows the last
four characters. Creating one needs the password re-entered within ``reauth_window_minutes``
(403 REAUTHENTICATION_REQUIRED otherwise), so a stolen 15-minute access token cannot mint a
long-lived credential.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi import status as http

from climate import auth_service
from climate.api import auth
from climate.api.auth import OwnerCallerDep, OwnerDep, Principal, RecentAuthDep, Role
from climate.api.schemas import ApiTokenCreateBody, ApiTokenCreated, ApiTokenOut, AuditEventOut

router = APIRouter(tags=["tokens"])


@router.get("/tokens", response_model=list[ApiTokenOut])
def list_tokens(_: Role = OwnerDep) -> list[ApiTokenOut]:
    """Every token, newest first, revoked ones included (never the secret)."""
    return [ApiTokenOut.model_validate(t, from_attributes=True) for t in auth_service.list_api_tokens()]


@router.post("/tokens", response_model=ApiTokenCreated, status_code=http.HTTP_201_CREATED)
def create_token(body: ApiTokenCreateBody, request: Request, principal: Principal = RecentAuthDep) -> ApiTokenCreated:
    """Mint a token; the response is the only time the full token is ever shown."""
    row, raw = auth_service.create_api_token(
        principal,
        name=body.name,
        role=body.role,
        expires_in_days=body.expires_in_days,
        local_only=body.local_only,
        ip=auth.client_ip(request),
    )
    out = ApiTokenOut.model_validate(row, from_attributes=True)
    return ApiTokenCreated(**out.model_dump(), token=raw)


@router.delete("/tokens/{token_id}", status_code=http.HTTP_204_NO_CONTENT, response_class=Response)
def revoke_token(token_id: int, request: Request, principal: Principal = OwnerCallerDep) -> Response:
    """Revoke a token; it stops working on its next use (404 when there is no such live token)."""
    if not auth_service.revoke_api_token(principal, token_id, ip=auth.client_ip(request)):
        raise auth.auth_error(http.HTTP_404_NOT_FOUND, "NOT_FOUND", "No such active token.")
    return Response(status_code=http.HTTP_204_NO_CONTENT)


@router.get("/audit", response_model=list[AuditEventOut])
def list_audit(
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    _: Role = OwnerDep,
) -> list[AuditEventOut]:
    """Recent sign-in, device and token events, newest first (payloads never hold secrets)."""
    return [
        AuditEventOut.model_validate(e, from_attributes=True)
        for e in auth_service.list_audit(event_type=event_type or None, limit=limit)
    ]

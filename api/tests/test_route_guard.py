"""The route guard: every route the app serves, against every kind of caller.

Spec: docs/specs/users-and-tokens.md ("Who may call what"). ``ROUTES`` classifies every
(method, path) the app registers, including FastAPI's docs, the WebSocket and the static web
app. A route that is missing from it fails ``test_every_route_is_classified``, so a new route
cannot ship without someone deciding who may call it; a route classified as protected that is
actually reachable without the right credential fails ``test_the_matrix_holds``.

Callers: the owner (an access token from sign-in), API tokens of each role (agent, viewer,
control), the legacy ``CLIMATE_AGENT_TOKEN`` (role agent, from a private address) and
anonymous. Expected outcome per class:

- allowed      anything except an auth-layer 401/403 (the request itself may still be a 422,
               404 or 409: the bodies are empty on purpose so nothing changes)
- not allowed  403 ``FORBIDDEN``
- anonymous    401 ``NOT_AUTHENTICATED``
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from climate.api import auth as auth_mod
from climate.api.routers import ws as ws_mod
from tests.conftest import AGENT_TOKEN, OWNER_PASSWORD, make_client, mint_token

# --- the matrix -----------------------------------------------------------------------------

CALLERS = ("owner", "agent", "viewer", "control", "legacy")
ALLOWED: dict[str, set[str]] = {
    "reader": {"owner", "agent", "viewer", "control", "legacy"},
    "any": {"owner", "agent", "viewer", "control", "legacy"},
    "control": {"owner", "control"},
    "writer": {"owner", "agent", "legacy"},
    "agent": {"agent", "legacy"},
    "owner": {"owner"},
    "recent": {"owner"},  # owner with the password re-entered recently
}

# The ONLY routes reachable without a credential (the spec's list, plus FastAPI's generated
# API docs, which describe the API but return no data).
PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/state"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/openapi.json"),
    ("GET", "/api/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/{path:path}"),  # the static web app (index.html fallback; /api/* is a 404 there)
    ("MOUNT", "/assets"),  # the web app's built assets
}

ROUTES: dict[tuple[str, str], str] = {
    # auth
    ("GET", "/api/auth/me"): "any",
    ("POST", "/api/auth/ws-ticket"): "any",
    ("POST", "/api/auth/reauth"): "owner",
    ("POST", "/api/auth/change-password"): "owner",
    ("GET", "/api/auth/sessions"): "owner",
    ("DELETE", "/api/auth/sessions/{session_id}"): "owner",
    ("POST", "/api/auth/logout-all"): "owner",
    ("WS", "/api/ws"): "any",
    # tokens and audit
    ("GET", "/api/tokens"): "owner",
    ("POST", "/api/tokens"): "recent",
    ("DELETE", "/api/tokens/{token_id}"): "owner",
    ("GET", "/api/audit"): "owner",
    # status and analytics
    ("GET", "/api/status"): "reader",
    ("GET", "/api/rooms/{room_key}/history"): "reader",
    ("GET", "/api/runtime/daily"): "reader",
    ("GET", "/api/runtime/intraday"): "reader",
    ("GET", "/api/weather"): "reader",
    ("GET", "/api/analytics/savings"): "reader",
    ("GET", "/api/analytics/waterfall"): "reader",
    ("GET", "/api/analytics/baselines"): "reader",
    ("GET", "/api/analytics/coupling"): "reader",
    ("GET", "/api/analytics/comfort"): "reader",
    ("GET", "/api/analytics/drift"): "reader",
    ("GET", "/api/analytics/natural-experiments"): "reader",
    # control
    ("GET", "/api/control/settings"): "reader",
    ("PUT", "/api/control/settings"): "owner",
    ("POST", "/api/control/mode"): "owner",
    ("GET", "/api/control/plan"): "reader",
    ("GET", "/api/control/actions"): "reader",
    ("POST", "/api/control/hold"): "control",
    ("POST", "/api/control/resume"): "control",
    ("POST", "/api/control/automatic"): "control",
    ("POST", "/api/control/presence"): "control",
    ("GET", "/api/control/handback"): "reader",
    ("POST", "/api/control/handback"): "owner",
    ("GET", "/api/changes"): "reader",
    ("POST", "/api/changes"): "writer",
    ("POST", "/api/changes/{change_id}/decision"): "writer",
    ("GET", "/api/utility-events"): "reader",
    ("POST", "/api/utility-events/{event_id}/skip"): "control",
    ("POST", "/api/utility-events/{event_id}/unskip"): "control",
    # experiments and models
    ("GET", "/api/experiments"): "reader",
    ("GET", "/api/experiments/power"): "reader",
    ("GET", "/api/experiments/{experiment_id}"): "reader",
    ("POST", "/api/experiments"): "writer",
    ("POST", "/api/experiments/{experiment_id}/decision"): "owner",
    ("GET", "/api/models"): "reader",
    ("POST", "/api/models/refit"): "writer",
    ("POST", "/api/models/backtest"): "writer",
    ("POST", "/api/models/simulate"): "writer",
    ("GET", "/api/jobs/{job_id}"): "reader",
    # reports and alerts
    ("GET", "/api/reports"): "reader",
    ("GET", "/api/reports/{report_id}"): "reader",
    ("POST", "/api/reports"): "writer",
    ("GET", "/api/alerts"): "reader",
    ("POST", "/api/alerts/{alert_id}/resolve"): "owner",
    # the agent
    ("GET", "/api/agent/status"): "reader",
    ("GET", "/api/agent/runs"): "reader",
    ("GET", "/api/agent/runs/{run_id}"): "reader",
    ("POST", "/api/agent/ask"): "owner",
    ("POST", "/api/agent/run"): "owner",
    ("POST", "/api/agent/claim"): "agent",
    ("POST", "/api/agent/runs/{run_id}/finish"): "agent",
    ("POST", "/api/agent/heartbeat"): "agent",
    # setup (owner only, reads included: it holds the house location and device addresses)
    ("GET", "/api/setup"): "owner",
    ("POST", "/api/setup/source"): "owner",
    ("PUT", "/api/setup/location"): "owner",
    ("POST", "/api/setup/ecobee/login"): "owner",
    ("POST", "/api/setup/ecobee/mfa"): "owner",
    ("POST", "/api/setup/ecobee/signout"): "owner",
    ("POST", "/api/setup/ecobee/map"): "owner",
    ("POST", "/api/setup/sensors/map"): "owner",
    ("POST", "/api/setup/homekit/pair"): "owner",
    ("POST", "/api/setup/homekit/code"): "owner",
    ("POST", "/api/setup/homekit/unpair"): "owner",
}

# The agent role must never reach these, whatever the table above says: no thermostat write,
# controller mode, settings, setup, tokens, devices or audit (reads of settings are fine).
NEVER_AGENT = re.compile(r"^/api/(setup|tokens|audit|auth/(sessions|logout-all|change-password|reauth))")
NEVER_AGENT_WRITE = re.compile(r"^/api/(control/(hold|resume|automatic|presence|mode|handback|settings)"
                               r"|utility-events/.+/(skip|unskip))")

# Ends every owner session, so the owner calls it last.
OWNER_LAST = {("POST", "/api/auth/logout-all")}
AUTH_CODES = {"NOT_AUTHENTICATED", "TOKEN_EXPIRED", "SESSION_REVOKED", "FORBIDDEN", "REAUTHENTICATION_REQUIRED"}


# --- walking the app ------------------------------------------------------------------------


def _flatten(app) -> Iterator[tuple[str, str, Any]]:
    """(method, path, dependant) for every route, with included routers expanded. FastAPI 0.142
    keeps included routers as nested objects in ``app.routes``; expand them to full paths."""
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            for ctx in contexts():
                orig = ctx.original_route
                path = ctx.path or getattr(ctx.starlette_route, "path", "") or route.include_context.prefix + orig.path
                yield from _expand(orig, path, ctx.dependant or getattr(orig, "dependant", None))
        else:
            yield from _expand(route, route.path, getattr(route, "dependant", None))


def _expand(route, path: str, dependant) -> Iterator[tuple[str, str, Any]]:
    kind = type(route).__name__
    if "WebSocket" in kind:
        yield "WS", path, dependant
    elif kind == "Mount":
        yield "MOUNT", path, None
    else:
        for method in sorted(route.methods or ()):
            if method != "HEAD":  # Starlette adds HEAD to every GET
                yield method, path, dependant


def _calls(dependant) -> set:
    out, stack = set(), [dependant]
    while stack:
        d = stack.pop()
        if d is None:
            continue
        out.add(d.call)
        stack.extend(d.dependencies)
    return out


@pytest.fixture
def web_app(monkeypatch, tmp_path, fresh_db):
    """The app with a (tiny) built web app, so the static routes are walked too."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Climate AI</title>")
    monkeypatch.setenv("CLIMATE_WEB_DIR", str(tmp_path))
    from climate.api.app import create_app

    return create_app()


def test_every_route_is_classified(web_app):
    routes = {(m, p) for m, p, _ in _flatten(web_app)}
    known = PUBLIC | set(ROUTES)
    assert routes == known, f"unclassified: {sorted(routes - known)}; stale: {sorted(known - routes)}"


def test_every_protected_route_depends_on_the_resolver(web_app):
    """Belt and braces: every non-public HTTP route has the caller resolver in its dependency
    tree, and the public ones do not require it."""
    missing, public_with_auth = [], []
    for method, path, dependant in _flatten(web_app):
        if method in ("WS", "MOUNT"):
            continue
        calls = _calls(dependant) if dependant is not None else set()
        if (method, path) in PUBLIC:
            if auth_mod.current_caller in calls:
                public_with_auth.append((method, path))
        elif auth_mod.current_caller not in calls:
            missing.append((method, path))
    assert not missing, f"no auth dependency: {missing}"
    assert not public_with_auth, f"public routes that require a caller: {public_with_auth}"


def test_the_agent_role_never_reaches_owner_surfaces():
    leaks = [(m, p) for (m, p), cls in ROUTES.items()
             if (NEVER_AGENT.match(p) or (m != "GET" and NEVER_AGENT_WRITE.match(p))) and "agent" in ALLOWED[cls]]
    assert any(NEVER_AGENT_WRITE.match(p) for (_, p) in ROUTES)  # the patterns still match real routes
    assert not leaks, leaks


# --- the behavioural walk -------------------------------------------------------------------


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: "hallway" if m.group(1) == "room_key" else "999999", path)


def _auth_failure(r) -> str | None:
    """The auth-layer error code of a response, or None when the auth layer let it through."""
    if r.status_code not in (401, 403):
        return None
    try:
        detail = r.json().get("detail")
    except ValueError:
        return None
    if isinstance(detail, dict) and detail.get("code") in AUTH_CODES:
        return detail["code"]
    return None


def _ws_outcome(c, headers: dict[str, str]) -> str:
    try:
        with c.websocket_connect("/api/ws", headers=headers) as sock:
            assert sock.receive_json()["type"] == "status"
            return "ok"
    except WebSocketDisconnect as exc:
        return f"closed {exc.code}"


@pytest.fixture
def callers(client) -> dict[str, dict[str, str]]:
    """Authorization headers per caller, all from a private address (127.0.0.1)."""
    r = client.post("/api/auth/setup", json={"password": OWNER_PASSWORD, "device_name": "guard"})
    assert r.status_code == 200, r.text
    owner = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.cookies.clear()  # bearer only: the refresh cookie plays no part in the matrix
    me = client.get("/api/auth/me", headers=owner).json()
    assert me["role"] == "owner" and me["recently_authenticated"] is True  # sign-in counts as recent
    return {
        "owner": owner,
        "agent": {"Authorization": f"Bearer {mint_token('agent')}"},
        "viewer": {"Authorization": f"Bearer {mint_token('viewer')}"},
        "control": {"Authorization": f"Bearer {mint_token('control')}"},
        "legacy": {"Authorization": f"Bearer {AGENT_TOKEN}"},
    }


def test_the_matrix_holds(client, callers):
    failures: list[str] = []
    deferred: list[tuple[str, str, str]] = []
    for (method, path), cls in ROUTES.items():
        if method == "WS":
            if (got := _ws_outcome(client, {})) != f"closed {ws_mod.CLOSE_UNAUTHORIZED}":
                failures.append(f"anonymous WS {path}: {got}")
            for who in CALLERS:
                want = "ok" if who in ALLOWED[cls] else f"closed {ws_mod.CLOSE_FORBIDDEN}"
                if (got := _ws_outcome(client, callers[who])) != want:
                    failures.append(f"{who} WS {path}: {got}, want {want}")
            continue
        url = _concrete(path)
        r = client.request(method, url, json={})
        if r.status_code != 401 or _auth_failure(r) != "NOT_AUTHENTICATED":
            failures.append(f"anonymous {method} {path}: {r.status_code} {r.text[:120]}")
        for who in CALLERS:
            if who == "owner" and (method, path) in OWNER_LAST:
                deferred.append((method, path, cls))
                continue
            r = client.request(method, url, json={}, headers=callers[who])
            code = _auth_failure(r)
            if who in ALLOWED[cls]:
                if code is not None:
                    failures.append(f"{who} {method} {path}: refused {r.status_code} {code}")
            elif r.status_code != 403 or code != "FORBIDDEN":
                failures.append(f"{who} {method} {path}: {r.status_code} {r.text[:120]}, want 403 FORBIDDEN")
    for method, path, _cls in deferred:
        r = client.request(method, _concrete(path), json={}, headers=callers["owner"])
        if _auth_failure(r) is not None:
            failures.append(f"owner {method} {path}: refused {r.status_code} {r.text[:120]}")
    assert not failures, "\n".join(failures)


def test_public_routes_answer_anonymous_callers(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/state").status_code == 200
    assert client.post("/api/auth/setup", json={}).status_code == 422
    assert client.post("/api/auth/login", json={}).status_code == 422
    assert client.post("/api/auth/logout").status_code == 204
    r = client.post("/api/auth/refresh")  # public, but it needs the refresh cookie
    assert r.status_code == 401 and r.json()["detail"]["code"] == "NOT_AUTHENTICATED"


def test_static_web_app_never_serves_api_paths(web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/").status_code == 200
        assert c.get("/security").status_code == 200  # history fallback
        r = c.get("/api/not-a-route")
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_tokens_from_the_internet_are_refused_unless_allowed(fresh_db):
    """``local_only`` tokens and the legacy env token only work from private addresses."""
    from tests.conftest import PUBLIC_ADDR

    local = mint_token("viewer")
    remote = mint_token("viewer", local_only=False)
    with make_client(addr=PUBLIC_ADDR) as c:
        for token in (local, AGENT_TOKEN):
            r = c.get("/api/status", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN", r.text
        assert c.get("/api/status", headers={"Authorization": f"Bearer {remote}"}).status_code == 200

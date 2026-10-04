"""API client: any token format is sent as-is, refused tokens come back with the fix spelled
out, and an internal API URL is called directly even when the host sets an HTTP proxy."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from climate_agent.api import ApiClient, ApiError, SyncApiClient, is_internal_url, normalize_token

CAI_TOKEN = "cai_1f_" + "A1b2C3d4" * 5  # shaped like an app-made API token
LEGACY_TOKEN = "3f9a0c7e-legacy-agent-token"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _sent_authorization(token: str) -> str:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})

    with SyncApiClient("http://app:8000", token, transport=httpx.MockTransport(handler)) as api:
        assert api.get("/status") == {"ok": True}
    return seen[0]


@pytest.mark.parametrize("token", [CAI_TOKEN, LEGACY_TOKEN, "short"])
def test_any_token_format_is_sent_verbatim(token):
    assert _sent_authorization(token) == f"Bearer {token}"
    assert _sent_authorization(f"  Bearer {token}\n") == f"Bearer {token}"  # pasted with the prefix


def test_malformed_or_missing_token_is_refused_before_sending():
    assert normalize_token("  bearer abc ") == "abc"
    with pytest.raises(ValueError, match="spaces, line breaks"):
        normalize_token("abc def")
    with pytest.raises(ValueError, match="spaces, line breaks"):
        ApiClient("http://app:8000", "abc\ndef")
    with pytest.raises(ValueError, match="non-ASCII"):
        normalize_token("tökén")
    with pytest.raises(ValueError, match="token is required"):
        ApiClient("http://app:8000", "   ")


def _error(status: int, body: object, token_name: str = "CLIMATE_AGENT_TOKEN") -> ApiError:
    api = SyncApiClient(
        "http://app:8000", LEGACY_TOKEN, token_name=token_name,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    )
    with api, pytest.raises(ApiError) as exc:
        api.post("/agent/claim")
    return exc.value


def test_401_from_the_auth_layer_says_how_to_fix_it():
    err = _error(401, {"detail": {"code": "NOT_AUTHENTICATED", "message": "This API token has been revoked."}})
    assert err.code == "NOT_AUTHENTICATED" and err.is_auth_error
    assert err.detail == "This API token has been revoked. (NOT_AUTHENTICATED)"
    hint = err.auth_hint or ""
    assert "CLIMATE_AGENT_TOKEN" in hint and "More → Security → API tokens" in hint
    assert "scripts/bootstrap.sh put in .env as CLIMATE_AGENT_TOKEN" in hint
    # An older API's plain 401 is a token problem too.
    assert _error(401, {"detail": "Not authenticated"}).auth_hint


def test_403_auth_layer_vs_business_refusal():
    role = _error(403, {"detail": {"code": "FORBIDDEN", "message": "This API token only works from the home network."}},
                  token_name="CLIMATE_MCP_TOKEN")
    assert role.is_auth_error and "agent role" in (role.auth_hint or "") and "CLIMATE_MCP_TOKEN" in (role.auth_hint or "")
    business = _error(403, {"detail": "Claude may not decide this change: outside sign-off range"})
    assert business.code is None and not business.is_auth_error and business.auth_hint is None
    assert "outside sign-off range" in business.detail
    unconfigured = _error(503, {"detail": {"code": "AUTH_NOT_CONFIGURED", "message": "Sign-in is not configured."}})
    assert "climate.cli doctor" in (unconfigured.auth_hint or "")
    assert _error(500, {"detail": "boom"}).auth_hint is None


@pytest.mark.parametrize(
    ("url", "internal"),
    [
        ("http://app:8000", True),
        ("http://localhost:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("http://192.168.1.20:8470", True),
        ("http://100.101.2.3:8000", True),  # Tailscale
        ("http://climate.local:8470", True),
        ("https://box.tail1234.ts.net", True),
        ("https://climate.example.com", False),
        ("https://203.0.113.9", True),  # documentation range: not globally routable
        ("https://8.8.8.8", False),
    ],
)
def test_internal_url_classification(url, internal):
    assert is_internal_url(url) is internal


@pytest.fixture
def bogus_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy on the host that cannot reach the app (nothing listens on port 9)."""
    for name in PROXY_VARS:
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.fixture
def local_api() -> Iterator[str]:
    """A tiny stand-in API on loopback that answers GET /api/health."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"ok": True, "auth": self.headers.get("Authorization")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_internal_api_ignores_proxy_environment(bogus_proxy_env, local_api):
    # With the proxy honoured both calls would fail with "cannot reach the API".
    with SyncApiClient(local_api, CAI_TOKEN) as api:
        assert api.get("/health") == {"ok": True, "auth": f"Bearer {CAI_TOKEN}"}

    async def go():
        async with ApiClient(local_api, LEGACY_TOKEN) as api:
            return await api.get("/health")

    assert asyncio.run(go()) == {"ok": True, "auth": f"Bearer {LEGACY_TOKEN}"}


def test_public_api_keeps_proxy_environment(bogus_proxy_env):
    async def go():
        async with ApiClient("https://climate.example.com", CAI_TOKEN) as api:
            with pytest.raises(ApiError) as exc:
                await api.get("/health")
            return exc.value

    err = asyncio.run(go())
    assert err.status_code is None and "cannot reach the API" in err.detail  # went to the dead proxy

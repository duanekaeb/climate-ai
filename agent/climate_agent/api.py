"""Small HTTP client for the Climate AI API, authenticated with a bearer token.

Both an async client (tools, scheduler, MCP server) and a sync one (scripts) are provided.
Paths are given relative to ``/api`` (``"/status"``). Errors surface as ``ApiError`` with
the API's own ``detail`` text so a tool can hand it back to Claude.

The token is opaque here: a ``cai_...`` API token made in the app (More → Security → API
tokens, role ``agent``) and the legacy ``CLIMATE_AGENT_TOKEN`` / ``CLIMATE_MCP_TOKEN`` from
``.env`` are sent the same way, as ``Authorization: Bearer <token>``; the API decides what it
is. When the API refuses the token (401, or a 403 from its auth layer), ``ApiError.auth_hint``
says in plain words how to fix it.

Proxies: an API on an internal address (``http://app:8000``, ``localhost``, a private or
Tailscale IP, a single-label or ``.local`` / ``.lan`` / ``.internal`` / ``.home.arpa`` name)
is always called directly, ignoring ``HTTP(S)_PROXY`` / ``ALL_PROXY`` from the environment: a
proxy set on the host cannot reach the Compose network or the LAN and broke the agent's calls
to the app. A public HTTPS name keeps the usual environment handling (proxies, ``NO_PROXY``,
``SSL_CERT_FILE``).
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx

from climate_agent import __version__

DEFAULT_TIMEOUT_S = 20.0
DETAIL_MAX_CHARS = 600
AGENT_TOKEN_ENV = "CLIMATE_AGENT_TOKEN"
# What the owner can do about a refused token (the API's auth layer said 401 or 403).
TOKEN_HELP = (
    "Create an agent token in More → Security → API tokens, or keep the one scripts/bootstrap.sh "
    "put in .env as CLIMATE_AGENT_TOKEN"
)
RESTART_HELP = "then restart the service that reads it (with Docker: `docker compose up -d`)"
# Host name suffixes that only resolve inside the house, the Compose network or the tailnet.
_INTERNAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain", ".ts.net")


class ApiError(Exception):
    """An API call failed. ``status_code`` is None when the API could not be reached.

    ``code`` is the auth layer's machine code (``NOT_AUTHENTICATED``, ``FORBIDDEN``,
    ``AUTH_NOT_CONFIGURED``...) when the API answered ``{"detail": {"code", "message"}}``,
    else None (a plain ``{"detail": "..."}``, e.g. a sign-off outside Claude's ranges).
    ``token_name`` is the environment variable the refused token came from."""

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int | None,
        detail: str,
        *,
        code: str | None = None,
        token_name: str = AGENT_TOKEN_ENV,
    ):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.token_name = token_name
        where = f"{method} {path}"
        super().__init__(f"{where} -> {status_code}: {detail}" if status_code else f"{where}: {detail}")

    @property
    def is_client_error(self) -> bool:
        return self.status_code is not None and 400 <= self.status_code < 500

    @property
    def is_auth_error(self) -> bool:
        """The API refused the token itself, not the request: every 401, and a 403 or 503
        from the auth layer (a plain-text 403 is a business refusal, e.g. a sign-off outside
        Claude's ranges, and is not a token problem)."""
        if self.status_code == 401:
            return True
        return self.code is not None and self.status_code in (403, 503)

    @property
    def auth_hint(self) -> str | None:
        """What the owner should do about a refused token, or None for any other error."""
        if not self.is_auth_error:
            return None
        name = self.token_name
        if self.status_code == 401:
            return (
                f"The API did not accept the token in {name} (mistyped, revoked, expired, or made on "
                f"another server). {TOKEN_HELP}, {RESTART_HELP}."
            )
        if self.status_code == 403:
            return (
                f"The API refused the token in {name} for this: agent work needs a token with the agent role "
                "(a viewer or control token cannot do it), and a home-network-only token is refused from the "
                f"internet. {TOKEN_HELP}, {RESTART_HELP}."
            )
        return (
            "The API's sign-in is not configured yet (CLIMATE_JWT_SECRET / CLIMATE_TOKEN_PEPPER); run "
            "`docker compose exec app python -m climate.cli doctor` on the server."
        )


def api_base(url: str) -> str:
    """``http://app:8000`` -> ``http://app:8000/api`` (idempotent)."""
    base = url.strip().rstrip("/")
    if not base:
        raise ValueError("API URL is empty")
    return base if base.endswith("/api") else base + "/api"


def is_internal_url(url: str) -> bool:
    """True when ``url`` points at the Compose network, this machine, the LAN or the tailnet
    (where an HTTP proxy from the environment cannot help and usually breaks the call)."""
    host = (httpx.URL(url).host or "").strip("[]").rstrip(".").lower()
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_INTERNAL_SUFFIXES)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Not globally routable: private, loopback, link-local, and Tailscale's 100.64.0.0/10.
    return not ip.is_global


def normalize_token(raw: str) -> str:
    """The bearer token as configured, minus surrounding whitespace and an accidentally pasted
    ``Bearer `` prefix. No format is assumed (``cai_...`` and legacy tokens alike); a token
    with spaces, control or non-ASCII characters inside cannot be sent in a header and is
    refused."""
    token = (raw or "").strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    if any(ch.isspace() or not ch.isprintable() or not ch.isascii() for ch in token):
        raise ValueError(
            "the API token contains spaces, line breaks or non-ASCII characters; paste it exactly as the app showed it"
        )
    return token


def _error_detail(response: httpx.Response) -> tuple[str, str | None]:
    """``(text, code)`` of an error response. ``code`` is set for the auth layer's
    ``{"detail": {"code", "message"}}``."""
    try:
        body = response.json()
    except ValueError:
        return _format_detail(response), None
    detail: Any = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        message = str(detail.get("message") or detail["code"])
        return f"{message} ({detail['code']})"[:DETAIL_MAX_CHARS], detail["code"]
    return _format_detail(response), None


def _format_detail(response: httpx.Response) -> str:
    """FastAPI errors: {"detail": "text"}, {"detail": [{"loc": [...], "msg": "..."}]} or the
    auth layer's {"detail": {"code": "...", "message": "..."}}."""
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip() or response.reason_phrase
        return text[:DETAIL_MAX_CHARS]
    detail: Any = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict) and "message" in detail:
        detail = detail["message"]
    if isinstance(detail, list):
        parts: list[str] = []
        for item in detail[:8]:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                msg = str(item.get("msg", item))
                parts.append(f"{loc}: {msg}" if loc else msg)
            else:
                parts.append(str(item))
        text = "; ".join(parts)
    else:
        text = str(detail)
    return text[:DETAIL_MAX_CHARS]


def _headers(token: str) -> dict[str, str]:
    token = normalize_token(token)
    if not token:
        raise ValueError("an API bearer token is required")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"climate-agent/{__version__}",
    }


def _decode(method: str, path: str, response: httpx.Response, token_name: str = AGENT_TOKEN_ENV) -> Any:
    if response.status_code >= 400:
        detail, code = _error_detail(response)
        raise ApiError(method, path, response.status_code, detail, code=code, token_name=token_name)
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(method, path, response.status_code, "response was not JSON") from exc


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


class ApiClient:
    """Async client. Create once per process (or per run) and ``aclose()`` it."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        token_name: str = AGENT_TOKEN_ENV,
    ):
        self.base_url = api_base(base_url)
        self.token_name = token_name
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=_headers(token),
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            transport=transport,
            follow_redirects=False,
            trust_env=not is_internal_url(self.base_url),
        )

    def __repr__(self) -> str:
        return f"ApiClient({self.base_url!r})"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout_s: float | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"params": _clean_params(params)}
        if json is not None:
            kwargs["json"] = json
        if timeout_s is not None:
            kwargs["timeout"] = httpx.Timeout(timeout_s, connect=5.0)
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError(method, path, None, f"timed out ({type(exc).__name__})", token_name=self.token_name) from exc
        except httpx.TransportError as exc:
            raise ApiError(
                method, path, None, f"cannot reach the API at {self.base_url} ({type(exc).__name__})",
                token_name=self.token_name,
            ) from exc
        return _decode(method, path, response, self.token_name)

    async def get(self, path: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None) -> Any:
        return await self.request("GET", path, params=params, timeout_s=timeout_s)

    async def post(self, path: str, json: Any = None, *, params: dict[str, Any] | None = None, timeout_s: float | None = None) -> Any:
        return await self.request("POST", path, params=params, json=json, timeout_s=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class SyncApiClient:
    """Blocking twin of ``ApiClient`` for scripts and one-off checks."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        token_name: str = AGENT_TOKEN_ENV,
    ):
        self.base_url = api_base(base_url)
        self.token_name = token_name
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=_headers(token),
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            transport=transport,
            follow_redirects=False,
            trust_env=not is_internal_url(self.base_url),
        )

    def __repr__(self) -> str:
        return f"SyncApiClient({self.base_url!r})"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout_s: float | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"params": _clean_params(params)}
        if json is not None:
            kwargs["json"] = json
        if timeout_s is not None:
            kwargs["timeout"] = httpx.Timeout(timeout_s, connect=5.0)
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError(method, path, None, f"timed out ({type(exc).__name__})", token_name=self.token_name) from exc
        except httpx.TransportError as exc:
            raise ApiError(
                method, path, None, f"cannot reach the API at {self.base_url} ({type(exc).__name__})",
                token_name=self.token_name,
            ) from exc
        return _decode(method, path, response, self.token_name)

    def get(self, path: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None) -> Any:
        return self.request("GET", path, params=params, timeout_s=timeout_s)

    def post(self, path: str, json: Any = None, *, params: dict[str, Any] | None = None, timeout_s: float | None = None) -> Any:
        return self.request("POST", path, params=params, json=json, timeout_s=timeout_s)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SyncApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

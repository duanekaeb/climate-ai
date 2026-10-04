"""Small HTTP client for the Climate AI API, authenticated with a bearer token.

Both an async client (tools, scheduler, MCP server) and a sync one (scripts) are provided.
Paths are given relative to ``/api`` (``"/status"``). Errors surface as ``ApiError`` with
the API's own ``detail`` text so a tool can hand it back to Claude.
"""

from __future__ import annotations

from typing import Any

import httpx

from climate_agent import __version__

DEFAULT_TIMEOUT_S = 20.0
DETAIL_MAX_CHARS = 600


class ApiError(Exception):
    """An API call failed. ``status_code`` is None when the API could not be reached."""

    def __init__(self, method: str, path: str, status_code: int | None, detail: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        where = f"{method} {path}"
        super().__init__(f"{where} -> {status_code}: {detail}" if status_code else f"{where}: {detail}")

    @property
    def is_client_error(self) -> bool:
        return self.status_code is not None and 400 <= self.status_code < 500


def api_base(url: str) -> str:
    """``http://app:8000`` -> ``http://app:8000/api`` (idempotent)."""
    base = url.strip().rstrip("/")
    if not base:
        raise ValueError("API URL is empty")
    return base if base.endswith("/api") else base + "/api"


def _format_detail(response: httpx.Response) -> str:
    """FastAPI errors: {"detail": "text"} or {"detail": [{"loc": [...], "msg": "..."}]}."""
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip() or response.reason_phrase
        return text[:DETAIL_MAX_CHARS]
    detail: Any = body.get("detail", body) if isinstance(body, dict) else body
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
    if not token:
        raise ValueError("an API bearer token is required")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"climate-agent/{__version__}",
    }


def _decode(method: str, path: str, response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise ApiError(method, path, response.status_code, _format_detail(response))
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
    ):
        self.base_url = api_base(base_url)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=_headers(token),
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            transport=transport,
            follow_redirects=False,
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
            raise ApiError(method, path, None, f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise ApiError(method, path, None, f"cannot reach the API at {self.base_url} ({type(exc).__name__})") from exc
        return _decode(method, path, response)

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
    ):
        self.base_url = api_base(base_url)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=_headers(token),
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            transport=transport,
            follow_redirects=False,
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
            raise ApiError(method, path, None, f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise ApiError(method, path, None, f"cannot reach the API at {self.base_url} ({type(exc).__name__})") from exc
        return _decode(method, path, response)

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

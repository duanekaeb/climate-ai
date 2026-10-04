"""``python -m climate_agent.mcp_server [--http HOST:PORT]``: Claude's tools for Claude Code /
Claude Desktop, served with the mcp package's MCPServer (FastMCP, renamed in mcp 2.x).

Same toolkit as the agent service: read, compute and gated tools over the API, none of which
can reach a thermostat. Two tokens, two jobs:

- **Outbound, to the API:** ``CLIMATE_AGENT_TOKEN`` (falls back to ``CLIMATE_MCP_TOKEN``):
  any token the API accepts with the agent role, i.e. a ``cai_...`` agent token made in the
  app (More → Security → API tokens) or the legacy env token. Its format is never inspected.
- **Inbound, from MCP clients** (``--http`` only): ``CLIMATE_MCP_TOKEN`` (falls back to the
  API token). Every request must carry ``Authorization: Bearer <it>``. A client holding the
  API token could call the API directly with the same rights, so this grants nothing extra.

Local only: the HTTP transport answers 403 to any client that is not on this machine, the LAN,
Docker's networks or Tailscale (private, loopback, link-local and 100.64.0.0/10 addresses);
it is never put behind the public gateway. stdio has no listener at all.

- stdio (default): for ``claude mcp add house -- python -m climate_agent.mcp_server``.
- ``--http HOST:PORT``: streamable HTTP at ``/mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import inspect
import ipaddress
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from climate_agent import __version__
from climate_agent.api import AGENT_TOKEN_ENV, ApiClient, normalize_token
from climate_agent.config import DEFAULT_API_URL
from climate_agent.toolkit import TOOLS, Toolkit, ToolSpec, tool_parameters

log = logging.getLogger("climate_agent.mcp_server")

SERVER_NAME = "climate-house"
INSTRUCTIONS = (
    "Tools for analysing a three-thermostat home (main floor, upstairs, bed/office wing). Read tools return short "
    "text summaries (°F, stage-1 runtime minutes, 90% intervals). Never claim savings without the weather-normalized "
    "interval. Rooms without a sensor (Twins' Room, Olive's Room, Foyer) have unknown temperature; never estimate it. "
    "Gated tools propose changes, or approve or hold model changes inside pre-approved ranges (only the owner can "
    "reject); nothing here writes to a thermostat. "
    "Cite 'Weather data by Open-Meteo.com' when using weather."
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _tool_function(toolkit: Toolkit, spec: ToolSpec) -> Callable[..., Awaitable[str]]:
    """A function MCPServer can register: the tool's own signature, text out, errors raised
    as ToolError (sent to the client as an ``is_error`` result with the message)."""

    async def run_tool(**kwargs: Any) -> str:
        result = await toolkit.call(spec.name, kwargs)
        if result.is_error:
            raise ToolError(result.text)
        return result.text

    params = [p.replace(kind=inspect.Parameter.KEYWORD_ONLY) for p in tool_parameters(spec)]
    run_tool.__name__ = spec.name
    run_tool.__qualname__ = spec.name
    run_tool.__doc__ = spec.description
    run_tool.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    return run_tool


def build_server(toolkit: Toolkit) -> MCPServer:
    server: MCPServer = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)
    for spec in TOOLS:
        server.add_tool(
            _tool_function(toolkit, spec),
            name=spec.name,
            title=spec.name.replace("_", " "),
            description=spec.description,
            annotations=ToolAnnotations(readOnlyHint=spec.read_only, destructiveHint=False, openWorldHint=False),
            structured_output=False,
        )
    return server


MCP_TOKEN_ENV = "CLIMATE_MCP_TOKEN"
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def is_local_client(host: str | None) -> bool:
    """A client on this machine, the LAN, a Docker network or Tailscale. No address (a Unix
    socket, or an in-process test) counts as local."""
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in _TAILSCALE)


async def _send_json(send: Send, status: int, body: bytes, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *(extra or []),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class BearerAuth:
    """ASGI middleware: HTTP requests must come from a local address (``is_local_client``)
    and carry ``Authorization: Bearer <token>`` (constant-time compared)."""

    def __init__(self, app: ASGIApp, token: str):
        if not token:
            raise ValueError("a bearer token is required for the HTTP transport")
        self.app = app
        self._token = token.encode()

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() == b"authorization":
                raw = bytes(value)
                if raw[:7].lower() == b"bearer ":
                    return hmac.compare_digest(raw[7:].strip(), self._token)
                return False
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") in ("http", "websocket"):
            client = scope.get("client")
            if not is_local_client(client[0] if client else None):
                if scope["type"] == "http":
                    await _send_json(
                        send, 403,
                        b'{"detail":"The MCP server only answers this machine, the home network and Tailscale."}',
                    )
                return
            if scope["type"] == "http" and not self._authorized(scope):
                await _send_json(
                    send, 401,
                    b'{"detail":"Bearer token required (CLIMATE_MCP_TOKEN on the MCP server)."}',
                    [(b"www-authenticate", b"Bearer")],
                )
                return
        await self.app(scope, receive, send)


def parse_hostport(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host.strip("[]"), int(port)


def _env_token(env: Mapping[str, str], name: str) -> str:
    return normalize_token(env.get(name) or "")


def api_token_source(env: Mapping[str, str]) -> tuple[str, str]:
    """``(variable name, token)`` the tools call the API with: ``CLIMATE_AGENT_TOKEN``, else
    ``CLIMATE_MCP_TOKEN`` (a legacy MCP token the API also accepts). ``("", "")`` when unset."""
    for name in (AGENT_TOKEN_ENV, MCP_TOKEN_ENV):
        token = _env_token(env, name)
        if token:
            return name, token
    return "", ""


def api_token(env: Mapping[str, str]) -> str:
    """The token the tools send to the API (see ``api_token_source``)."""
    return api_token_source(env)[1]


def inbound_token(env: Mapping[str, str]) -> str:
    """The bearer token MCP clients must present over HTTP: ``CLIMATE_MCP_TOKEN``, else the
    API token."""
    return _env_token(env, MCP_TOKEN_ENV) or api_token(env)


def build_http_app(server: MCPServer, host: str, token: str) -> BearerAuth:
    return BearerAuth(server.streamable_http_app(host=host, streamable_http_path="/mcp"), token)


async def _serve(server: MCPServer, api: ApiClient, http: tuple[str, int] | None, client_token: str) -> None:
    try:
        if http is None:
            await server.run_stdio_async()
            return
        import uvicorn

        host, port = http
        config = uvicorn.Config(build_http_app(server, host, client_token), host=host, port=port, log_level="info")
        log.info(
            "serving MCP over streamable HTTP at http://%s:%d/mcp (local clients only, bearer token required)",
            host, port,
        )
        await uvicorn.Server(config).serve()
    finally:
        await api.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="climate_agent.mcp_server", description=__doc__.split("\n\n")[0])
    parser.add_argument("--http", type=parse_hostport, metavar="HOST:PORT", help="serve streamable HTTP instead of stdio")
    args = parser.parse_args(argv)
    # stdio carries the protocol on stdout: logs go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        token_name, token = api_token_source(os.environ)
        client_token = inbound_token(os.environ)
    except ValueError as exc:
        log.error("bad token: %s", exc)
        return 2
    if not token:
        log.error(
            "set CLIMATE_AGENT_TOKEN (or CLIMATE_MCP_TOKEN) so the tools can reach the API: create an agent token "
            "in More → Security → API tokens, or use the one scripts/bootstrap.sh put in .env"
        )
        return 2
    api = ApiClient(os.environ.get("CLIMATE_API_URL") or DEFAULT_API_URL, token, token_name=token_name)
    log.info("calling the API at %s with the token in %s", api.base_url, token_name)
    server = build_server(Toolkit(api))
    asyncio.run(_serve(server, api, args.http, client_token))
    return 0


if __name__ == "__main__":
    sys.exit(main())

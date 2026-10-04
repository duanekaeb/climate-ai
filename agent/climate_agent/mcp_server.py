"""``python -m climate_agent.mcp_server [--http HOST:PORT]``: Claude's tools for Claude Code /
Claude Desktop, served with the mcp package's MCPServer (FastMCP, renamed in mcp 2.x).

Same toolkit as the agent service: read, compute and gated tools over the API, none of which
can reach a thermostat. It authenticates to the API with ``CLIMATE_MCP_TOKEN`` (falls back to
``CLIMATE_AGENT_TOKEN``), which gives it the API's agent role.

- stdio (default): for ``claude mcp add house -- python -m climate_agent.mcp_server``.
- ``--http HOST:PORT``: streamable HTTP at ``/mcp``. Every request must carry
  ``Authorization: Bearer <the same token>``; anyone holding it could call the API directly
  with the same rights, so this grants nothing extra.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import inspect
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from climate_agent import __version__
from climate_agent.api import ApiClient
from climate_agent.config import DEFAULT_API_URL
from climate_agent.toolkit import TOOLS, Toolkit, ToolSpec, tool_parameters

log = logging.getLogger("climate_agent.mcp_server")

SERVER_NAME = "climate-house"
INSTRUCTIONS = (
    "Tools for analysing a three-thermostat home (main floor, upstairs, bed/office wing). Read tools return short "
    "text summaries (°F, stage-1 runtime minutes, 90% intervals). Never claim savings without the weather-normalized "
    "interval. Rooms without a sensor (Twins' Room, Olive's Room, Foyer) have unknown temperature; never estimate it. "
    "Gated tools propose changes or sign them off inside pre-approved ranges; nothing here writes to a thermostat. "
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


class BearerAuth:
    """ASGI middleware: HTTP requests need ``Authorization: Bearer <token>``."""

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
        if scope.get("type") == "http" and not self._authorized(scope):
            body = b'{"detail":"Bearer token required."}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def parse_hostport(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host.strip("[]"), int(port)


def api_token(env: Mapping[str, str]) -> str:
    return (env.get("CLIMATE_MCP_TOKEN") or "").strip() or (env.get("CLIMATE_AGENT_TOKEN") or "").strip()


def build_http_app(server: MCPServer, host: str, token: str) -> BearerAuth:
    return BearerAuth(server.streamable_http_app(host=host, streamable_http_path="/mcp"), token)


async def _serve(server: MCPServer, api: ApiClient, http: tuple[str, int] | None, token: str) -> None:
    try:
        if http is None:
            await server.run_stdio_async()
            return
        import uvicorn

        host, port = http
        config = uvicorn.Config(build_http_app(server, host, token), host=host, port=port, log_level="info")
        log.info("serving MCP over streamable HTTP at http://%s:%d/mcp (bearer token required)", host, port)
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
    token = api_token(os.environ)
    if not token:
        log.error("set CLIMATE_MCP_TOKEN (or CLIMATE_AGENT_TOKEN) so the tools can reach the API")
        return 2
    api = ApiClient(os.environ.get("CLIMATE_API_URL") or DEFAULT_API_URL, token)
    server = build_server(Toolkit(api))
    asyncio.run(_serve(server, api, args.http, token))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""MCP server: every toolkit tool is registered (same names as the agent's SDK server), calls
reach the API, errors surface as tool errors, and the HTTP transport demands the bearer token."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import SAVINGS
from mcp.server.mcpserver.exceptions import ToolError

from climate_agent.mcp_server import BearerAuth, api_token, build_http_app, build_server, parse_hostport
from climate_agent.toolkit import TOOLS, Toolkit
from climate_agent.tools import allowed_tool_names, build_tools


def test_registers_every_tool(make_api):
    api, _ = make_api({})
    server = build_server(Toolkit(api))

    async def go():
        try:
            return await server.list_tools()
        finally:
            await api.aclose()

    tools = asyncio.run(go())
    names = [t.name for t in tools]
    assert names == [t.name for t in TOOLS]
    assert [f"mcp__house__{n}" for n in names] == allowed_tool_names()
    assert [t.name for t in build_tools(Toolkit(api))] == names
    by_name = {t.name: t for t in tools}
    assert by_name["get_house_status"].annotations.read_only_hint is True
    assert by_name["sign_off_change"].annotations.read_only_hint is False
    props = by_name["sign_off_change"].input_schema["properties"]
    assert set(props) == {"change_id", "decision", "reason"}
    assert props["decision"]["enum"] == ["approve", "hold"]  # Claude may not reject (the API returns 403)
    assert "cannot reject" in by_name["sign_off_change"].description
    sdk = {t.name: t for t in build_tools(Toolkit(api))}
    assert sdk["sign_off_change"].input_schema["properties"]["decision"]["enum"] == ["approve", "hold"]
    assert set(by_name["propose_experiment"].input_schema["required"]) == {"name", "hypothesis", "arms"}


def test_tool_call_and_error(make_api):
    api, router = make_api({("GET", "/api/analytics/savings"): SAVINGS})
    server = build_server(Toolkit(api))

    async def go():
        try:
            ok = await server.call_tool("savings_report", {})
            with pytest.raises(ToolError) as exc:
                await server.call_tool("propose_policy_change", {"title": "abc", "rationale": "r", "params": {"nope": 1}})
            return ok, str(exc.value)
        finally:
            await api.aclose()

    ok, err = asyncio.run(go())
    assert "90% interval [2.0%, 12.3%]" in ok.content[0].text and not ok.is_error
    assert "Unknown policy parameter" in err
    assert router.paths() == ["GET /api/analytics/savings"]
    assert router.requests[0].headers["authorization"] == "Bearer test-agent-token"


def test_sdk_tool_handler_marks_errors(make_api):
    api, _ = make_api({})
    tools = {t.name: t for t in build_tools(Toolkit(api))}

    async def go():
        try:
            return await tools["sign_off_change"].handler({"change_id": 1, "decision": "approve", "reason": "fine"})
        finally:
            await api.aclose()

    out = asyncio.run(go())
    assert out["is_error"] is True and "Not found (404)" in out["content"][0]["text"]


async def _asgi(app: Any, headers: list[tuple[bytes, bytes]]) -> tuple[int, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "http", "method": "POST", "path": "/mcp", "headers": headers}, receive, send)
    return sent[0]["status"], sent


def test_http_requires_bearer_token():
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BearerAuth(inner, "mcp-token")
    assert asyncio.run(_asgi(app, []))[0] == 401
    assert asyncio.run(_asgi(app, [(b"authorization", b"Bearer wrong")]))[0] == 401
    assert asyncio.run(_asgi(app, [(b"authorization", b"Basic mcp-token")]))[0] == 401
    assert asyncio.run(_asgi(app, [(b"authorization", b"Bearer mcp-token")]))[0] == 200


def test_http_app_and_args(make_api):
    api, _ = make_api({})
    app = build_http_app(build_server(Toolkit(api)), "0.0.0.0", "mcp-token")
    assert isinstance(app, BearerAuth)
    assert parse_hostport("0.0.0.0:8765") == ("0.0.0.0", 8765)
    assert parse_hostport("[::1]:9000") == ("::1", 9000)
    with pytest.raises(Exception):
        parse_hostport("8765")
    assert api_token({"CLIMATE_MCP_TOKEN": "m", "CLIMATE_AGENT_TOKEN": "a"}) == "m"
    assert api_token({"CLIMATE_AGENT_TOKEN": "a"}) == "a"
    asyncio.run(api.aclose())

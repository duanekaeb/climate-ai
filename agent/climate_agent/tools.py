"""The toolkit as an in-process MCP server for the Claude Agent SDK (server name ``house``).

Claude sees the tools as ``mcp__house__<name>``. Every handler returns text; API refusals
and bad arguments come back with ``is_error`` so Claude can adapt instead of the run failing.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, ToolAnnotations, create_sdk_mcp_server, tool

from climate_agent import __version__
from climate_agent.toolkit import TOOLS, Toolkit, ToolSpec, input_schema

SERVER_NAME = "house"


def mcp_tool_name(name: str) -> str:
    return f"mcp__{SERVER_NAME}__{name}"


def allowed_tool_names() -> list[str]:
    """Fully qualified names to pre-approve; nothing else is allowed in ``dontAsk`` mode."""
    return [mcp_tool_name(spec.name) for spec in TOOLS]


def _annotations(spec: ToolSpec) -> ToolAnnotations:
    return ToolAnnotations(
        title=spec.name.replace("_", " "),
        readOnlyHint=spec.read_only,
        destructiveHint=False,
        openWorldHint=False,
    )


def _make_tool(toolkit: Toolkit, spec: ToolSpec) -> SdkMcpTool[Any]:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = await toolkit.call(spec.name, args)
        return {"content": [{"type": "text", "text": result.text}], "is_error": result.is_error}

    return tool(spec.name, spec.description, input_schema(spec.name), annotations=_annotations(spec))(handler)


def build_tools(toolkit: Toolkit) -> list[SdkMcpTool[Any]]:
    return [_make_tool(toolkit, spec) for spec in TOOLS]


def create_house_server(toolkit: Toolkit) -> McpSdkServerConfig:
    return create_sdk_mcp_server(name=SERVER_NAME, version=__version__, tools=build_tools(toolkit))

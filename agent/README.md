# climate-agent

Claude Opus 5.5 as the house's analyst, through the Claude Agent SDK on the owner's Claude
**subscription** (no API key, no per-token bill). It verifies yesterday's decisions, signs off
or holds model-queued changes inside pre-approved ranges, proposes changes and experiments, and
writes reports. It is never in the control path and has no tool that writes to a thermostat.
It has no database credentials: it talks to the API with a bearer token (role `agent`).

- `python -m climate_agent.scheduler`: the agent service. Checks sign-in once at start,
  heartbeats every 60 s, claims a queued run every 20 s, runs it, reports the result.
- `python -m climate_agent.mcp_server [--http HOST:PORT]`: the same tools for Claude Code or
  Claude Desktop (stdio by default; streamable HTTP at `/mcp` with a bearer token).

## Subscription setup (once)

1. On any machine with Claude Code: `claude setup-token` (browser approval) prints a one-year
   token.
2. Set it as `CLAUDE_CODE_OAUTH_TOKEN` for the agent service, and `CLAUDE_TOKEN_CREATED` to
   today's date so the app can warn 30 days before it expires.
3. **Never set `ANTHROPIC_API_KEY`** (or `ANTHROPIC_AUTH_TOKEN`, or a Bedrock/Vertex/Foundry
   switch) on the server: it takes priority over the subscription and bills the API. The agent
   refuses to start while one is set, and aborts any run whose sign-in reports an API key.
4. **Never use `--bare` mode** (`CLAUDE_CODE_SIMPLE`): it ignores subscription sign-in. The
   agent refuses to start with it.

## Environment

| Variable | Default | |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | required | from `claude setup-token`; read by the CLI, never logged |
| `CLAUDE_TOKEN_CREATED` | unset | `YYYY-MM-DD`; expiry = +1 year, warned within 30 days |
| `CLIMATE_API_URL` | `http://app:8000` | the API (paths under `/api`) |
| `CLIMATE_AGENT_TOKEN` | required | bearer token for the API's agent role |
| `CLIMATE_AGENT_MODEL` | `claude-opus-5-5` | |
| `CLIMATE_AGENT_FALLBACK_MODEL` | `claude-sonnet-5-5` | used on overload, and for a rerun when only the Opus limit is hit |
| `CLIMATE_AGENT_CWD` | `/srv/climate/agent-empty` | created if missing; must stay empty |
| `CLIMATE_AGENT_MAX_TURNS` | `30` | capped at 30 |
| `CLIMATE_MCP_TOKEN` | falls back to `CLIMATE_AGENT_TOKEN` | MCP server's API token (and its HTTP bearer token) |

## How a run is isolated

No built-in tools (`tools=[]`, and Bash/Read/Write/Edit/Glob/Grep/Agent/NotebookEdit/WebFetch/
WebSearch disallowed), only `mcp__house__*` pre-approved, `permission_mode="dontAsk"`, no
filesystem settings (`setting_sources=[]`), only our MCP server, an empty working directory,
at most 30 turns. A run's digest is trusted only when `terminal_reason == "completed"`. A
usage limit (429) defers the run until the limit resets; if only the Opus limit was hit, the
run is retried once on the fallback model.

## Claude Code

```bash
claude mcp add house --env CLIMATE_API_URL=https://climate.example --env CLIMATE_MCP_TOKEN=... \
  -- python -m climate_agent.mcp_server
# or, against a running `--http 0.0.0.0:8765` service:
claude mcp add --transport http house http://server:8765/mcp --header "Authorization: Bearer ..."
```

## Tests

```bash
pip install -e '.[test]' && pytest -q
```

Tests never call Claude or the network: the API is an `httpx.MockTransport` and the runner gets
a fake `query` function.

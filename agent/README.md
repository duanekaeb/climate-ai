# climate-agent

Claude Opus 5.5 as the house's analyst, through the Claude Agent SDK on the owner's Claude
**subscription** (no API key, no per-token bill). It verifies yesterday's decisions, signs off
or holds model-queued changes inside pre-approved ranges, proposes changes and experiments, and
writes reports. It is never in the control path and has no tool that writes to a thermostat.
It has no database credentials: it talks to the API with a bearer token that has the `agent`
role (see [The API token](#the-api-token)).

- `python -m climate_agent.scheduler`: the agent service. Checks sign-in once at start,
  heartbeats every 60 s, claims a queued run every 20 s, runs it, reports the result (every
  claimed run is finished, even if running it crashes). While sign-in fails it claims nothing,
  keeps heartbeating "not signed in" and re-checks at most every 30 minutes; a run that fails
  sign-in is deferred 30 minutes, not failed.
- `python -m climate_agent.mcp_server [--http HOST:PORT]`: the same tools for Claude Code or
  Claude Desktop (stdio by default; streamable HTTP at `/mcp` with a bearer token, local
  clients only).

## The API token

There is one login for people (the owner password); services such as this agent use API
tokens. Either of these works as `CLIMATE_AGENT_TOKEN`, and nothing in the agent depends on
which one it is (the token is sent as `Authorization: Bearer <token>`; the API decides):

- **An agent token made in the app** (recommended): More → Security → API tokens → create,
  role **agent**, keep "home network only" on. It looks like `cai_..._...`, is shown once, and
  can be revoked from the same page; the page shows when it was last used.
- **The legacy env token** that `scripts/bootstrap.sh` put in `.env` as `CLIMATE_AGENT_TOKEN`.
  The API accepts it as the agent role from private addresses only (home network, Docker,
  Tailscale).

The agent role reads everything the tools summarize, proposes changes and experiments, signs
off or holds model-queued changes inside its ranges, and claims and finishes its own runs. It
can never write to a thermostat, change the controller mode or settings, run set-up, or touch
tokens. A `viewer` or `control` token cannot run the agent (its claims are refused).

After changing the token in `.env`, recreate the service so it reads it: `docker compose up -d`.

**When the API refuses the token** the agent says how to fix it instead of failing quietly:

- At start it asks the API who it is (`GET /api/auth/me`) and logs `API token accepted (agent
  role, ...)`, or an error naming the problem (refused, or a token with another role).
- A 401 (mistyped, revoked, expired, or made on another server) or a 403 from the auth layer
  (wrong role, or a home-network-only token used from the internet) is logged as an error that
  names the variable and says "Create an agent token in More → Security → API tokens, or keep
  the one scripts/bootstrap.sh put in .env as CLIMATE_AGENT_TOKEN". It is logged when it first
  appears and then every 30 minutes, not on every 20-second retry.
- A tool that hits a refused token tells Claude the same, so a chat answer or report says what
  to fix. A plain 403 (e.g. approving a change outside Claude's ranges) is a refusal of that
  request, not a token problem, and Claude leaves it for the owner.

**Proxies.** The internal API URL (`http://app:8000`, `localhost`, a private or Tailscale IP, a
single-label, `.local`, `.lan`, `.internal`, `.home.arpa` or `.ts.net` name) is always called
directly: `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` set on the host are ignored for it (a
host proxy cannot reach the Compose network and broke these calls). A public HTTPS URL keeps the
usual environment handling (`NO_PROXY`, `SSL_CERT_FILE`).

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
| `CLIMATE_AGENT_TOKEN` | required | API token with the agent role: a `cai_...` agent token from the app, or the legacy one from `scripts/bootstrap.sh` |
| `CLIMATE_AGENT_MODEL` | `claude-opus-5-5` | |
| `CLIMATE_AGENT_FALLBACK_MODEL` | `claude-sonnet-5-5` | used on overload, and for a rerun when only the Opus limit is hit |
| `CLIMATE_AGENT_CWD` | `/srv/climate/agent-empty` | created if missing; must stay empty |
| `CLIMATE_AGENT_MAX_TURNS` | `30` | capped at 30 |
| `CLIMATE_MCP_TOKEN` | falls back to `CLIMATE_AGENT_TOKEN` | the bearer token MCP clients must send to `--http`; also the MCP server's API token when `CLIMATE_AGENT_TOKEN` is unset (the API accepts the legacy MCP token as the agent role) |

## How a run is isolated

No built-in tools (`tools=[]`, and Bash/Read/Write/Edit/Glob/Grep/Agent/NotebookEdit/WebFetch/
WebSearch disallowed), only `mcp__house__*` pre-approved, `permission_mode="dontAsk"`, no
filesystem settings (`setting_sources=[]`), only our MCP server, an empty working directory,
at most 30 turns, and prompts delivered verbatim (`verbatim_prompts=True`: no `@path`
expansion of the owner's chat text or a trigger's JSON). A run's digest is trusted only when
`terminal_reason == "completed"`. A usage limit (429) defers the run until the limit resets;
if only the Opus limit was hit, the run is retried once on the fallback model.

A retry (that fallback retry, or a deferred or handed-back run claimed again) resumes the
earlier Claude session when its id is known (the fallback retry, or a run this process
deferred; the API's run record does not carry it) and its transcript is still on disk.
Either way its prompt lists what the run already did (reports with its `agent_run_id`; changes, experiments and sign-offs Claude made
since the run was queued), so it does not publish or propose them twice.

Claude may approve or hold model-proposed changes inside its sign-off ranges; it cannot reject
(the API refuses). Harm is recorded as a hold with the evidence in the reason; the owner
decides rejections.

## Claude Code

The MCP server stays local: stdio runs on your own machine, and the HTTP transport answers only
clients on that machine, the home network, Docker's networks or Tailscale (anything else gets
403, before the token is even checked). It is never put behind the public gateway. Two tokens,
two jobs:

- **To the API** it sends `CLIMATE_AGENT_TOKEN` (else `CLIMATE_MCP_TOKEN`): an agent token from
  More → Security → API tokens works, as does the legacy one.
- **From MCP clients** (`--http` only) it requires `Authorization: Bearer <CLIMATE_MCP_TOKEN>`
  (else the API token).

```bash
# stdio, on a machine at home (or on Tailscale): point it at the app on the LAN
claude mcp add house --env CLIMATE_API_URL=http://192.168.1.20:8470 --env CLIMATE_AGENT_TOKEN=cai_... \
  -- python -m climate_agent.mcp_server
# or, against the running `mcp` service (docker compose --profile mcp up -d):
claude mcp add --transport http house http://server:8471/mcp --header "Authorization: Bearer <CLIMATE_MCP_TOKEN>"
```

A home-network-only token is refused when the API sees a public address, so away from home use
Tailscale rather than the public HTTPS name. The local-address check sees whatever address
Docker hands the container, so keep `MCP_BIND` on `127.0.0.1` or a Tailscale/LAN address.

## Tests

```bash
pip install -e '.[test]' && pytest -q
```

Tests never call Claude or the network: the API is an `httpx.MockTransport` (or, for the proxy
test, a tiny server on 127.0.0.1) and the runner gets a fake `query` function.

# syntax=docker/dockerfile:1
#
# Climate AI agent image: the Claude Agent SDK runner (`python -m climate_agent.scheduler`)
# and the optional MCP server (`python -m climate_agent.mcp_server --http 0.0.0.0:8471`).
# Build context is the repository root; ONLY the agent/ package is copied in. No API code,
# no .env, no database client, no repository files reach this image.
#
# claude-agent-sdk's Linux wheels bundle the Claude Code CLI binary, so no Node is needed.
# Claude runs on the owner's subscription via CLAUDE_CODE_OAUTH_TOKEN (set at runtime by
# docker-compose.yml). ANTHROPIC_API_KEY must never be set; the agent refuses to start if it is.

ARG PYTHON_IMAGE=python:3.12-slim

# --------------------------------------------------------------------------------------
# 1. Build the agent package and its pinned SDK into /opt/venv
# --------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN pip install --upgrade pip setuptools wheel
COPY agent/ /src/agent/
RUN pip install /src/agent
# Fail the build loudly if the packaging is incomplete: the bundled CLI must be present and
# the prompts must have been installed as package data (not left behind in the source tree).
RUN python - <<'PY'
import importlib.metadata as m
import pathlib

import claude_agent_sdk
import climate_agent

print("claude-agent-sdk", m.version("claude-agent-sdk"))
sdk_dir = pathlib.Path(claude_agent_sdk.__file__).parent
bundled = [p for p in sdk_dir.rglob("claude") if p.is_file()]
assert bundled, f"no bundled Claude Code CLI under {sdk_dir}; was an sdist installed instead of a wheel?"
print("bundled CLI:", bundled[0])
prompts = pathlib.Path(climate_agent.__file__).parent / "prompts"
assert prompts.is_dir() and any(prompts.iterdir()), (
    f"{prompts} is missing or empty: declare the prompts as package data in agent/pyproject.toml"
)
print("prompts:", sorted(p.name for p in prompts.iterdir()))
PY

# --------------------------------------------------------------------------------------
# 2. Runtime: non-root, empty working directory, read-only friendly
# --------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="climate-ai-agent" \
      org.opencontainers.image.description="Climate AI Claude agent (Agent SDK) and MCP server" \
      org.opencontainers.image.licenses="Private"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/agent \
    TZ=UTC \
    CLIMATE_AGENT_CWD=/srv/climate/agent-empty \
    DISABLE_AUTOUPDATER=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# HOME (/home/agent) holds the CLI's ~/.claude and ~/.claude.json. docker-compose.yml mounts
# a tmpfs over it (the root filesystem is read-only), so the uid/gid here must match the
# tmpfs options there (10002). /srv/climate/agent-empty is Claude's working directory and
# must stay empty: no file is within reach of the model.
RUN groupadd --system --gid 10002 agent \
 && useradd --system --uid 10002 --gid agent --create-home --home-dir /home/agent \
        --shell /usr/sbin/nologin agent \
 && mkdir -p /srv/climate/agent-empty \
 && chown agent:agent /srv/climate/agent-empty \
 && chmod 700 /srv/climate/agent-empty /home/agent

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv/climate/agent-empty
USER agent

CMD ["python", "-m", "climate_agent.scheduler"]

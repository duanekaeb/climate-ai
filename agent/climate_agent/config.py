"""Agent configuration from environment variables, and the startup safety checks.

The OAuth token itself is never stored here: the Claude Code CLI reads
``CLAUDE_CODE_OAUTH_TOKEN`` from the inherited environment. This module only checks that it
is present, so no config object, repr or log line can ever carry it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from climate_agent.api import TOKEN_HELP, normalize_token

DEFAULT_API_URL = "http://app:8000"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_FALLBACK_MODEL = "claude-sonnet-5-5"
DEFAULT_CWD = "/srv/climate/agent-empty"
MAX_TURNS_CAP = 30
TOKEN_WARN_DAYS = 30

# Any of these routes Claude Code away from the owner's subscription (an API key or a
# gateway token takes priority and bills per token; the provider switches bill a cloud
# account). The agent refuses to start while one is set.
BILLING_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
# ``--bare`` mode (env form CLAUDE_CODE_SIMPLE) turns first-party sign-in off.
BARE_MODE_ENV_VAR = "CLAUDE_CODE_SIMPLE"
OAUTH_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


class ConfigError(ValueError):
    """A configuration value is missing or malformed."""


class StartupRefused(RuntimeError):
    """The environment is unsafe for a subscription-billed agent; refuse to start."""


def _clean_token(raw: str | None) -> str:
    """The configured API token, normalized (see ``api.normalize_token``). A token that can't
    be sent in a header is kept as typed so ``idle_reason`` reports it instead of crashing."""
    try:
        return normalize_token(raw or "")
    except ValueError:
        return (raw or "").strip()


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in ("0", "false", "no", "off", "")


def add_one_year(d: date) -> date:
    """Same calendar day next year; 29 February maps to 28 February."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


@dataclass(frozen=True)
class AgentConfig:
    api_url: str = DEFAULT_API_URL
    agent_token: str = ""
    oauth_token_present: bool = False
    token_created: date | None = None
    model: str = DEFAULT_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    cwd: Path = Path(DEFAULT_CWD)
    max_turns: int = MAX_TURNS_CAP
    heartbeat_s: float = 60.0
    claim_s: float = 20.0
    run_timeout_s: float = 1800.0
    api_timeout_s: float = 20.0
    log_level: str = "INFO"

    def __repr__(self) -> str:  # never print the bearer token
        return (
            f"AgentConfig(api_url={self.api_url!r}, model={self.model!r}, "
            f"fallback_model={self.fallback_model!r}, cwd={str(self.cwd)!r}, max_turns={self.max_turns}, "
            f"token_created={self.token_created}, oauth_token_present={self.oauth_token_present})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        env = os.environ if env is None else env
        created_raw = (env.get("CLAUDE_TOKEN_CREATED") or "").strip()
        token_created: date | None = None
        if created_raw:
            try:
                token_created = date.fromisoformat(created_raw)
            except ValueError as exc:
                raise ConfigError(f"CLAUDE_TOKEN_CREATED must be YYYY-MM-DD, got {created_raw!r}") from exc
        try:
            max_turns = int(env.get("CLIMATE_AGENT_MAX_TURNS", str(MAX_TURNS_CAP)))
        except ValueError as exc:
            raise ConfigError("CLIMATE_AGENT_MAX_TURNS must be an integer") from exc
        return cls(
            api_url=(env.get("CLIMATE_API_URL") or DEFAULT_API_URL).strip(),
            agent_token=_clean_token(env.get("CLIMATE_AGENT_TOKEN")),
            oauth_token_present=bool((env.get(OAUTH_ENV_VAR) or "").strip()),
            token_created=token_created,
            model=(env.get("CLIMATE_AGENT_MODEL") or DEFAULT_MODEL).strip(),
            fallback_model=(env.get("CLIMATE_AGENT_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL).strip(),
            cwd=Path(env.get("CLIMATE_AGENT_CWD") or DEFAULT_CWD),
            max_turns=max(1, min(MAX_TURNS_CAP, max_turns)),
            log_level=(env.get("CLIMATE_LOG_LEVEL") or "INFO").upper(),
        )

    @property
    def token_expires_at(self) -> date | None:
        return add_one_year(self.token_created) if self.token_created else None

    def token_warning(self, today: date) -> str | None:
        """A warning within TOKEN_WARN_DAYS of expiry (or after it), else None."""
        expires = self.token_expires_at
        if expires is None:
            return None
        left = (expires - today).days
        if left < 0:
            return f"The Claude sign-in token expired on {expires.isoformat()}. Run `claude setup-token` and update CLAUDE_CODE_OAUTH_TOKEN."
        if left <= TOKEN_WARN_DAYS:
            return (
                f"The Claude sign-in token expires on {expires.isoformat()} ({left} days). "
                "Run `claude setup-token`, update CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_TOKEN_CREATED."
            )
        return None


def startup_problems(env: Mapping[str, str], config: AgentConfig) -> list[str]:
    """Reasons the agent must not start. Empty list = safe to start."""
    problems: list[str] = []
    for name in BILLING_ENV_VARS:
        if (env.get(name) or "").strip():
            problems.append(
                f"{name} is set. It would take priority over the Claude subscription and bill the API; unset it."
            )
    if _truthy(env.get(BARE_MODE_ENV_VAR)):
        problems.append(f"{BARE_MODE_ENV_VAR} is set (--bare mode), which ignores subscription sign-in; unset it.")
    return problems


def idle_reason(config: AgentConfig) -> str | None:
    """Why the agent must idle instead of running Claude (None = ready).

    A missing token is not a reason to exit: the agent service is on by default, so exiting
    would make Docker restart it in a loop. It idles and reports "not signed in" instead."""
    if not config.agent_token:
        return f"CLIMATE_AGENT_TOKEN is not set, so the agent cannot reach the API. {TOKEN_HELP}."
    try:
        normalize_token(config.agent_token)
    except ValueError as exc:
        return f"CLIMATE_AGENT_TOKEN is malformed: {exc}. {TOKEN_HELP}."
    if not config.oauth_token_present:
        return f"{OAUTH_ENV_VAR} is not set. Run `claude setup-token` and set it for the agent service."
    return None


def check_startup(env: MutableMapping[str, str] | None = None, config: AgentConfig | None = None) -> AgentConfig:
    """Validate the environment and return the config, or raise StartupRefused when anything
    would bill the API instead of the subscription. Missing tokens are reported by
    :func:`idle_reason` (the service idles rather than exiting).

    An empty ``ANTHROPIC_API_KEY=`` (common from compose files) is removed from the process
    environment so the Claude Code subprocess never inherits it.
    """
    env = os.environ if env is None else env
    for name in BILLING_ENV_VARS:
        if name in env and not (env.get(name) or "").strip():
            del env[name]
    config = config or AgentConfig.from_env(env)
    problems = startup_problems(env, config)
    if problems:
        raise StartupRefused(" ".join(problems))
    return config


def ensure_empty_dir(path: Path) -> Path:
    """Create ``path`` (0700) if missing and assert it is an empty directory.

    Claude runs with this as its working directory so no file is within reach."""
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"agent working directory {path} is not a directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise RuntimeError(f"agent working directory {path} is not empty; refusing to run Claude there")
    return path


__all__ = [
    "BILLING_ENV_VARS",
    "AgentConfig",
    "ConfigError",
    "StartupRefused",
    "add_one_year",
    "check_startup",
    "ensure_empty_dir",
    "idle_reason",
    "startup_problems",
]

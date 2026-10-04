"""Run one queued agent run through the Claude Agent SDK and map it to ``AgentFinishBody``.

Isolation, by construction:
- no built-in tools (``tools=[]``) and the dangerous ones disallowed outright, because
  ``dontAsk`` still runs tools that never prompt; only ``mcp__house__*`` is pre-approved;
- no filesystem settings (``setting_sources=[]``), only our MCP server
  (``strict_mcp_config``), and an empty working directory;
- subscription billing only: no API key or ``--bare`` mode reaches the CLI, and a run whose
  init message reports an API-key source is aborted before it can spend anything;
- prompts are delivered verbatim (``verbatim_prompts``): no ``@path`` expansion or slash
  commands from the owner's chat text or a trigger's JSON.

A digest is trusted only when ``ResultMessage.terminal_reason == "completed"``. A usage limit
(HTTP 429) defers the run until the limit resets; if only the Opus limit was hit, the run is
retried once, immediately, on the fallback model. A sign-in failure defers the run 30 minutes
(the scheduler stops claiming until sign-in works again).

A retry (the immediate fallback retry, or a deferred / handed-back run claimed again) resumes
the earlier Claude session when its transcript is still on disk, and is always told what the
run already published, proposed or signed off, so gated side effects are not repeated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLIConnectionError,
    McpSdkServerConfig,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    get_session_info,
    query,
)

from climate_agent import __version__
from climate_agent.api import ApiClient, ApiError
from climate_agent.config import (
    BARE_MODE_ENV_VAR,
    BILLING_ENV_VARS,
    OAUTH_ENV_VAR,
    AgentConfig,
    ensure_empty_dir,
)
from climate_agent.toolkit import Toolkit, clip, parse_ts
from climate_agent.tools import SERVER_NAME, allowed_tool_names, create_house_server

log = logging.getLogger("climate_agent.runner")

QueryFn = Callable[..., AsyncIterator[Any]]
RunKind = Literal["nightly", "weekly", "triggered", "chat", "signin_check"]
FinishStatus = Literal["completed", "failed", "deferred"]

PROMPTS_DIR = Path(__file__).parent / "prompts"
DISALLOWED_TOOLS: list[str] = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "NotebookEdit", "WebFetch", "WebSearch",
]
# apiKeySource values in the CLI's init message that mean per-token API billing.
API_BILLED_KEY_SOURCES = frozenset({"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"})
DEFAULT_DEFER = timedelta(hours=1)
SIGNIN_DEFER = timedelta(minutes=30)
PRIOR_REPORTS_LIMIT = 20
PRIOR_CHANGES_LIMIT = 200
# AgentRunOut fields a fresh claim leaves empty; any of them set means an earlier attempt
# finished this run (deferred, or handed back when the service stopped).
_ATTEMPT_HISTORY_FIELDS = ("not_before", "terminal_reason", "num_turns", "model", "error", "session_id")
RESULT_TEXT_MAX = 20000
ERROR_TEXT_MAX = 2000
SIGNIN_SYSTEM_PROMPT = "This is a sign-in check for a scheduled service. Reply with the single word OK."
_AUTH_PATTERN = re.compile(
    r"authenticat|oauth|invalid (api )?key|not logged in|/login|setup-token|unauthori[sz]ed|token (has )?expired",
    re.IGNORECASE,
)
_SECRET_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")


class SubscriptionGuardError(RuntimeError):
    """The run would bill the API (or bypass subscription sign-in); refuse it."""


@dataclass
class RunOutcome:
    status: FinishStatus
    result_text: str | None = None
    model: str | None = None
    terminal_reason: str | None = None
    num_turns: int | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None
    session_id: str | None = None
    not_before: datetime | None = None
    # Sign-in state learned from this run (None = this run says nothing about it).
    signed_in: bool | None = None
    opus_limited: bool = False

    def finish_body(self) -> dict[str, Any]:
        """JSON body for ``POST /agent/runs/{id}/finish`` (AgentFinishBody)."""
        return {
            "status": self.status,
            "result_text": self.result_text,
            "model": self.model,
            "terminal_reason": self.terminal_reason,
            "num_turns": self.num_turns,
            "usage": self.usage,
            "error": self.error,
            "session_id": self.session_id,
            "not_before": self.not_before.isoformat() if self.not_before else None,
        }


@dataclass
class _Collected:
    result: ResultMessage | None = None
    rate_limit: RateLimitInfo | None = None
    last_text: str = ""
    model: str | None = None
    api_key_source: str | None = None
    session_id: str | None = None
    auth_error: bool = False
    rate_limited: bool = False
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=8))


# ---------------------------------------------------------------------------------------
# guards and helpers
# ---------------------------------------------------------------------------------------


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Strip anything token-shaped (and the configured secrets verbatim) from ``text``."""
    env = os.environ if env is None else env
    out = _SECRET_PATTERN.sub("[redacted]", text)
    for name in (OAUTH_ENV_VAR, "CLIMATE_AGENT_TOKEN", "CLIMATE_MCP_TOKEN", *BILLING_ENV_VARS):
        secret = (env.get(name) or "").strip()
        if len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    return out[:ERROR_TEXT_MAX]


def assert_subscription_env(env: Mapping[str, str] | None = None) -> None:
    """Raise unless the process environment signs Claude Code in with the subscription."""
    env = os.environ if env is None else env
    billing = [name for name in BILLING_ENV_VARS if (env.get(name) or "").strip()]
    if billing:
        raise SubscriptionGuardError(f"{', '.join(billing)} set: the run would bill the API instead of the subscription.")
    if (env.get(BARE_MODE_ENV_VAR) or "").strip().lower() not in ("", "0", "false", "no", "off"):
        raise SubscriptionGuardError(f"{BARE_MODE_ENV_VAR} (--bare mode) is set; it ignores subscription sign-in.")
    if not (env.get(OAUTH_ENV_VAR) or "").strip():
        raise SubscriptionGuardError(f"{OAUTH_ENV_VAR} is not set; run `claude setup-token`.")


def verify_options(options: ClaudeAgentOptions) -> None:
    """Last check before spawning Claude: isolation and subscription billing hold."""
    forbidden = [k for k in (*BILLING_ENV_VARS, BARE_MODE_ENV_VAR) if k in options.env]
    if forbidden:
        raise SubscriptionGuardError(f"options.env must not carry {', '.join(forbidden)}")
    if any(flag.lstrip("-") == "bare" for flag in options.extra_args):
        raise SubscriptionGuardError("--bare mode ignores subscription sign-in; never pass it")
    if options.tools != []:
        raise SubscriptionGuardError("built-in tools must be disabled (tools=[])")
    if options.setting_sources != []:
        raise SubscriptionGuardError("filesystem settings must not load (setting_sources=[])")
    if options.permission_mode != "dontAsk":
        raise SubscriptionGuardError("permission_mode must be 'dontAsk'")
    if options.verbatim_prompts is not True:
        raise SubscriptionGuardError("prompts must be delivered verbatim (verbatim_prompts=True): no @path expansion")
    if any(not name.startswith(f"mcp__{SERVER_NAME}__") for name in options.allowed_tools):
        raise SubscriptionGuardError("only mcp__house__* tools may be pre-approved")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def system_prompt_for(kind: str) -> str:
    if kind == "signin_check":
        return SIGNIN_SYSTEM_PROMPT
    if kind not in ("nightly", "weekly", "triggered", "chat"):
        raise ValueError(f"unknown run kind {kind!r}")
    return load_prompt("system") + "\n\n---\n\n" + load_prompt(kind)


def user_prompt_for(run: Mapping[str, Any], now: datetime) -> str:
    kind = run.get("kind")
    run_id = run.get("id")
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    extra = str(run.get("prompt") or "").strip()
    if kind == "signin_check":
        return "Sign-in check: reply with the single word OK."
    if kind == "chat":
        if not extra:
            raise ValueError("a chat run needs the owner's question in 'prompt'")
        return f"(Run #{run_id}, {stamp}.) The owner asks:\n\n{extra}"
    if kind == "triggered":
        trigger = json.dumps(run.get("trigger") or {}, indent=1, default=str, sort_keys=True)
        if len(trigger) > 6000:
            trigger = trigger[:6000] + "\n…(truncated)"
        text = (
            f"Triggered run #{run_id} at {stamp}: the system flagged an anomaly. Trigger details:\n\n"
            f"```json\n{trigger}\n```\n\nDiagnose it following your triggered-run instructions."
        )
        return text + (f"\n\nNote from the queue: {extra}" if extra else "")
    if kind in ("nightly", "weekly"):
        text = f"{kind.capitalize()} run #{run_id}, started {stamp}. Work through your {kind} checklist."
        return text + (f"\n\nOwner's note for this run: {extra}" if extra else "")
    raise ValueError(f"unknown run kind {kind!r}")


def resume_prompt_for(run: Mapping[str, Any], now: datetime, why: str) -> str:
    """The user turn that continues a resumed session (the original task is in its history)."""
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"(Run #{run.get('id')} resumed at {stamp}: {why}.) Carry on with the original task from where it "
        "stopped and finish it. Anything you fetched before may be out of date; re-check what you rely on."
    )


def subprocess_env() -> dict[str, str]:
    """Extra environment for the Claude Code subprocess. Never an API key: the CLI inherits
    ``CLAUDE_CODE_OAUTH_TOKEN`` from this process, which ``assert_subscription_env`` checked."""
    return {"CLAUDE_AGENT_SDK_CLIENT_APP": f"climate-ai-agent/{__version__}"}


def build_options(
    kind: str,
    config: AgentConfig,
    server: McpSdkServerConfig | None,
    *,
    model: str | None = None,
    stderr: Callable[[str], None] | None = None,
    resume: str | None = None,
) -> ClaudeAgentOptions:
    signin = kind == "signin_check"
    chosen = model or (config.fallback_model if signin else config.model)
    fallback = None if chosen == config.fallback_model else config.fallback_model
    if signin:
        effort: Literal["low", "medium", "high"] = "low"
    elif kind == "weekly":
        effort = "high"
    else:
        effort = "medium"
    return ClaudeAgentOptions(
        model=chosen,
        fallback_model=fallback,
        effort=effort,
        system_prompt=system_prompt_for(kind),
        mcp_servers={SERVER_NAME: server} if server is not None else {},
        strict_mcp_config=True,
        tools=[],
        allowed_tools=[] if signin else allowed_tool_names(),
        disallowed_tools=list(DISALLOWED_TOOLS),
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=str(config.cwd),
        max_turns=1 if signin else config.max_turns,
        env=subprocess_env(),
        stderr=stderr,
        # The owner's chat text and trigger JSON go to Claude exactly as written: no @path
        # file-mention expansion, no slash-command dispatch.
        verbatim_prompts=True,
        resume=None if signin else resume,
    )


# ---------------------------------------------------------------------------------------
# running and mapping
# ---------------------------------------------------------------------------------------


async def _consume(query_fn: QueryFn, prompt: str, options: ClaudeAgentOptions, col: _Collected) -> None:
    async for msg in query_fn(prompt=prompt, options=options):
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                source = msg.data.get("apiKeySource")
                col.api_key_source = source if isinstance(source, str) else None
                if isinstance(msg.data.get("model"), str):
                    col.model = msg.data["model"]
                if isinstance(msg.data.get("session_id"), str) and msg.data["session_id"]:
                    col.session_id = msg.data["session_id"]
                if col.api_key_source in API_BILLED_KEY_SOURCES:
                    raise SubscriptionGuardError(
                        f"Claude Code signed in with an API key (apiKeySource={col.api_key_source}); aborted to avoid API billing."
                    )
        elif isinstance(msg, RateLimitEvent):
            col.session_id = msg.session_id or col.session_id
            info = msg.rate_limit_info
            if col.rate_limit is None or info.status == "rejected" or col.rate_limit.status != "rejected":
                col.rate_limit = info
        elif isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id is None:
                text = "\n".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
                if text:
                    col.last_text = text
                col.model = msg.model or col.model
            if msg.error == "authentication_failed":
                col.auth_error = True
            elif msg.error == "rate_limit":
                col.rate_limited = True
        elif isinstance(msg, ResultMessage):
            col.result = msg
            col.session_id = msg.session_id or col.session_id


def _usage(r: ResultMessage) -> dict[str, Any]:
    usage: dict[str, Any] = dict(r.usage or {})
    usage["duration_ms"] = r.duration_ms
    usage["duration_api_ms"] = r.duration_api_ms
    if r.model_usage:
        usage["model_usage"] = r.model_usage
    if r.total_cost_usd is not None:
        usage["api_equivalent_cost_usd"] = r.total_cost_usd  # notional: billed to the subscription
    return usage


def _not_before(info: RateLimitInfo | None, now: datetime) -> datetime:
    if info is not None:
        for stamp in (info.resets_at, info.overage_resets_at):
            if isinstance(stamp, (int, float)) and stamp > 0:
                when = datetime.fromtimestamp(stamp, tz=timezone.utc)
                if when > now:
                    return when + timedelta(minutes=1)
    return now + DEFAULT_DEFER


def _is_opus_only(info: RateLimitInfo | None, text: str) -> bool:
    if info is not None and info.rate_limit_type is not None:
        return info.rate_limit_type == "seven_day_opus"
    low = text.lower()
    return "opus" in low and "limit" in low


def _base(col: _Collected, model: str) -> dict[str, Any]:
    r = col.result
    return {
        "model": col.model or model,
        "num_turns": r.num_turns if r else None,
        "session_id": (r.session_id if r else None) or col.session_id,
        "usage": _usage(r) if r else None,
    }


def _usage_limited(col: _Collected, model: str, text: str, terminal_reason: str | None, now: datetime) -> RunOutcome:
    info = col.rate_limit
    when = _not_before(info, now)
    window = info.rate_limit_type if info and info.rate_limit_type else "usage"
    return RunOutcome(
        status="deferred",
        terminal_reason=terminal_reason or "api_error",
        error=redact(f"Claude {window} limit reached; deferred until {when.isoformat()}. {text}".strip()),
        not_before=when,
        signed_in=True,
        opus_limited=_is_opus_only(info, text),
        **_base(col, model),
    )


def _failed(col: _Collected, model: str, text: str, terminal_reason: str | None, api_status: int | None) -> RunOutcome:
    auth = api_status in (401, 403) or col.auth_error or (api_status is None and bool(_AUTH_PATTERN.search(text)))
    if auth:
        text = f"Claude sign-in failed: {text} Run `claude setup-token` and update CLAUDE_CODE_OAUTH_TOKEN."
    return RunOutcome(
        status="failed",
        terminal_reason=terminal_reason,
        error=redact(text or "Claude run failed"),
        signed_in=False if auth else None,
        **_base(col, model),
    )


def _from_collected(col: _Collected, model: str, now: datetime) -> RunOutcome:
    r = col.result
    if r is None:
        return _failed(col, model, "Claude ended without a result message.", None, None)
    text = "; ".join(r.errors or []) or (r.result or "") or r.subtype
    if r.api_error_status == 429 or (r.is_error and col.rate_limited):
        return _usage_limited(col, model, text, r.terminal_reason, now)
    if r.is_error or r.terminal_reason != "completed":
        reason = r.terminal_reason or r.subtype
        return _failed(col, model, f"Run did not complete ({reason}): {text}", reason, r.api_error_status)
    result_text = (r.result or col.last_text or "").strip()
    if len(result_text) > RESULT_TEXT_MAX:
        result_text = result_text[:RESULT_TEXT_MAX] + "\n…(truncated)"
    return RunOutcome(
        status="completed",
        result_text=result_text,
        terminal_reason=r.terminal_reason,
        signed_in=True,
        **_base(col, model),
    )


def _from_result_error(exc: ResultError, col: _Collected, model: str, now: datetime) -> RunOutcome:
    text = exc.result or "; ".join(exc.errors) or str(exc)
    status = exc.api_error_status
    if status is None and col.result is not None:
        status = col.result.api_error_status
    if col.session_id is None and isinstance(exc.session_id, str) and exc.session_id:
        col.session_id = exc.session_id
    if status == 429 or (status is None and col.rate_limited):
        return _usage_limited(col, model, text, exc.terminal_reason, now)
    reason = exc.terminal_reason or exc.subtype
    out = _failed(col, model, f"Claude reported an error ({reason}): {text}", reason, status)
    if out.session_id is None:
        out.session_id = exc.session_id
    return out


async def _attempt(
    query_fn: QueryFn,
    prompt: str,
    kind: str,
    config: AgentConfig,
    server: McpSdkServerConfig | None,
    model: str | None,
    now: Callable[[], datetime],
    *,
    resume: str | None = None,
) -> RunOutcome:
    col = _Collected()
    options = build_options(kind, config, server, model=model, stderr=col.stderr_tail.append, resume=resume)
    chosen = options.model or config.model
    try:
        if kind == "signin_check":
            _verify_signin_options(options)
        else:
            verify_options(options)
        await asyncio.wait_for(_consume(query_fn, prompt, options, col), timeout=config.run_timeout_s)
    except ResultError as exc:
        return _from_result_error(exc, col, chosen, now())
    except SubscriptionGuardError as exc:
        return RunOutcome(status="failed", error=redact(str(exc)), model=chosen)
    except TimeoutError:
        return _failed(col, chosen, f"Run timed out after {int(config.run_timeout_s)} s.", "timeout", None)
    except (ProcessError, CLIConnectionError, ClaudeSDKError) as exc:
        tail = " | ".join(col.stderr_tail)
        detail = f"{type(exc).__name__}: {exc}" + (f" (stderr: {tail})" if tail else "")
        return _failed(col, chosen, detail, None, None)
    except Exception as exc:  # noqa: BLE001 - the SDK also raises bare Exception
        # (initialize control-request timeout, an error control_response, reader errors).
        # CancelledError is a BaseException and still propagates (service stopping).
        tail = " | ".join(col.stderr_tail)
        detail = f"Claude Agent SDK error ({type(exc).__name__}): {exc}" + (f" (stderr: {tail})" if tail else "")
        return _failed(col, chosen, detail, None, None)
    return _from_collected(col, chosen, now())


def _verify_signin_options(options: ClaudeAgentOptions) -> None:
    verify_options(options)
    if options.mcp_servers or options.allowed_tools or options.max_turns != 1:
        raise SubscriptionGuardError("a sign-in check runs with no tools and one turn")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------------------
# retries: resume the earlier session, and never repeat a gated side effect
# ---------------------------------------------------------------------------------------


def was_attempted(run: Mapping[str, Any]) -> bool:
    """True when the claimed run carries history from an earlier attempt (it was deferred, or
    handed back when the service stopped), so its gated tools may already have run."""
    return any(run.get(name) not in (None, "") for name in _ATTEMPT_HISTORY_FIELDS)


def session_exists(session_id: str, cwd: Path) -> bool:
    """Whether the CLI still has this session's transcript on disk (``resume`` needs it)."""
    try:
        return get_session_info(session_id, directory=str(cwd)) is not None
    except Exception:  # noqa: BLE001 - unreadable means "start fresh"
        return False


async def _rows(api: ApiClient, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = await api.get(path, params)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _since(row: Mapping[str, Any], key: str, since: datetime) -> bool:
    ts = parse_ts(row.get(key))
    return ts is not None and ts >= since


async def prior_work_note(api: ApiClient, run: Mapping[str, Any], since: datetime) -> str:
    """A note for a retried run: the reports it published (``agent_run_id``) and the changes,
    experiments and sign-offs Claude made since the run was queued, so none is repeated."""
    run_id = run.get("id")
    done: list[str] = []
    unchecked: list[str] = []
    try:
        for r in await _rows(api, "/reports", {"limit": PRIOR_REPORTS_LIMIT}):
            if run_id is not None and r.get("agent_run_id") == run_id:
                done.append(f"published {r.get('kind')} report #{r.get('id')} '{clip(r.get('title'), 80)}'")
    except ApiError:
        unchecked.append("reports")
    try:
        for c in await _rows(api, "/changes", {"limit": PRIOR_CHANGES_LIMIT}):
            if c.get("proposed_by") == "claude" and _since(c, "created_at", since):
                done.append(f"proposed change #{c.get('id')} '{clip(c.get('title'), 80)}' (now {c.get('status')})")
            if c.get("decided_by") == "claude" and _since(c, "decided_at", since):
                done.append(f"signed off change #{c.get('id')} '{clip(c.get('title'), 80)}' (now {c.get('status')})")
    except ApiError:
        unchecked.append("changes")
    try:
        for e in await _rows(api, "/experiments"):
            if e.get("proposed_by") == "claude" and _since(e, "created_at", since):
                done.append(f"proposed experiment #{e.get('id')} '{clip(e.get('name'), 80)}' (now {e.get('status')})")
    except ApiError:
        unchecked.append("experiments")
    lines = ["Retry note: an earlier attempt of this run stopped before it finished."]
    if done:
        lines.append(
            "Already done since this run was queued (by that attempt, or another Claude run). Do NOT publish, "
            "propose or sign off any of these again; build on them and mention them in your summary:"
        )
        lines += [f"- {item}" for item in done]
    elif not unchecked:
        lines.append("It published no report and made no proposal, experiment or sign-off, so nothing needs skipping.")
    if unchecked:
        lines.append(
            f"Could not check {', '.join(unchecked)} (API error). Before publishing, proposing or signing off "
            "anything, look with list_reports, list_experiments and review_pending_changes so nothing is done twice."
        )
    return "\n".join(lines)


async def _retry_prompt(
    api: ApiClient,
    run: Mapping[str, Any],
    prompt: str,
    session_id: str | None,
    config: AgentConfig,
    since: datetime,
    now: datetime,
    why: str,
) -> tuple[str, str | None]:
    """(prompt, resume session id) for a retry: resume when the session is still on disk,
    otherwise start fresh; either way, with the note of what is already done."""
    note = await prior_work_note(api, run, since)
    if session_id and await asyncio.to_thread(session_exists, session_id, config.cwd):
        return resume_prompt_for(run, now, why) + "\n\n" + note, session_id
    return prompt + "\n\n" + note, None


def _defer_signed_out(outcome: RunOutcome, now: datetime) -> RunOutcome:
    """A sign-in failure is not the run's fault: hand it back for after sign-in is fixed."""
    if outcome.signed_in is False and outcome.status == "failed":
        outcome.status = "deferred"
        outcome.not_before = now + SIGNIN_DEFER
        outcome.error = redact(
            f"{outcome.error or 'Claude sign-in failed.'} Deferred {int(SIGNIN_DEFER.total_seconds() // 60)} min; "
            "the run is retried once sign-in works."
        )
    return outcome


async def execute_run(
    run: Mapping[str, Any],
    *,
    config: AgentConfig,
    api: ApiClient,
    query_fn: QueryFn = query,
    now: Callable[[], datetime] = _utcnow,
    resume_session_id: str | None = None,
) -> RunOutcome:
    """Run one claimed run to an outcome. Never raises for Claude or API problems.

    ``resume_session_id`` (or a ``session_id`` on the run) names the session an earlier
    attempt of this run left; it is resumed when still on disk."""
    kind = str(run.get("kind") or "")
    try:
        assert_subscription_env()
        ensure_empty_dir(config.cwd)
        prompt = user_prompt_for(run, now())
    except (SubscriptionGuardError, RuntimeError, OSError, ValueError) as exc:
        return RunOutcome(status="failed", error=redact(str(exc)))
    if kind == "signin_check":
        return await _attempt(query_fn, prompt, kind, config, None, None, now)
    started = now()
    since = parse_ts(run.get("created_at")) or started - timedelta(days=1)
    earlier = resume_session_id or (run.get("session_id") if isinstance(run.get("session_id"), str) else None)
    first_prompt, first_resume = prompt, None
    if earlier or was_attempted(run):
        first_prompt, first_resume = await _retry_prompt(
            api, run, prompt, earlier, config, since, started, "an earlier attempt was deferred or interrupted"
        )
    server = create_house_server(Toolkit(api, run_id=run.get("id")))
    outcome = await _attempt(query_fn, first_prompt, kind, config, server, None, now, resume=first_resume)
    if outcome.status == "deferred" and outcome.opus_limited and config.model != config.fallback_model:
        log.warning("Opus usage limit reached; retrying run %s once on %s", run.get("id"), config.fallback_model)
        retry_prompt, retry_resume = await _retry_prompt(
            api, run, prompt, outcome.session_id or first_resume, config, since, now(),
            f"the Opus usage limit was reached, so it continues on {config.fallback_model}",
        )
        server = create_house_server(Toolkit(api, run_id=run.get("id")))
        retry = await _attempt(query_fn, retry_prompt, kind, config, server, config.fallback_model, now, resume=retry_resume)
        if retry.status == "deferred":
            retry.error = redact(f"Opus limit, then the fallback model's limit too. {retry.error or ''}")
        outcome = retry
    return _defer_signed_out(outcome, now())


async def run_one(
    run: Mapping[str, Any],
    query_fn: QueryFn = query,
    *,
    config: AgentConfig | None = None,
    api: ApiClient | None = None,
) -> dict[str, Any]:
    """Run ``run`` (an AgentRunOut dict) and return the body for ``/agent/runs/{id}/finish``."""
    config = config or AgentConfig.from_env()
    own_api = api is None
    client = api or ApiClient(config.api_url, config.agent_token, timeout_s=config.api_timeout_s)
    try:
        outcome = await execute_run(run, config=config, api=client, query_fn=query_fn)
    finally:
        if own_api:
            await client.aclose()
    return outcome.finish_body()

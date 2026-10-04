"""Runner: option construction (isolation, subscription) and result mapping, with a fake
``query`` standing in for the Claude Agent SDK. Claude is never called."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from conftest import change

from climate_agent import runner as runner_mod
from climate_agent.runner import (
    DISALLOWED_TOOLS,
    SubscriptionGuardError,
    build_options,
    execute_run,
    run_one,
    system_prompt_for,
    user_prompt_for,
    verify_options,
)
from climate_agent.toolkit import TOOLS, Toolkit
from climate_agent.tools import create_house_server

NOW = datetime(2026, 10, 4, 8, 30, tzinfo=UTC)
NIGHTLY = {"id": 42, "kind": "nightly", "prompt": "", "trigger": None}


def result_message(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success", "duration_ms": 61000, "duration_api_ms": 50000, "is_error": False,
        "num_turns": 9, "session_id": "sess-1", "result": "Nightly report published.",
        "terminal_reason": "completed", "usage": {"input_tokens": 1200, "output_tokens": 800},
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def init(api_key_source: str = "none", session_id: str | None = None) -> SystemMessage:
    data: dict[str, Any] = {"apiKeySource": api_key_source, "model": "claude-opus-5-5"}
    if session_id:
        data["session_id"] = session_id
    return SystemMessage(subtype="init", data=data)


def assistant(text: str, model: str = "claude-opus-5-5", error: Any = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model=model, error=error)


def usage_limit_error(text: str = "API Error: 429 usage limit reached", session_id: str = "sess-9") -> ResultError:
    data = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
            "terminal_reason": "api_error", "result": text, "session_id": session_id}
    return ResultError("Claude Code returned an error result: " + text, data=data, exit_code=1)


def auth_error() -> ResultError:
    return ResultError("auth", data={"subtype": "success", "is_error": True, "api_error_status": 401,
                                     "terminal_reason": "api_error", "result": "OAuth token has expired"}, exit_code=1)


def rate_limit(kind: str, resets_at: datetime | None, session_id: str = "sess-9") -> RateLimitEvent:
    info = RateLimitInfo(status="rejected", rate_limit_type=kind,  # type: ignore[arg-type]
                         resets_at=int(resets_at.timestamp()) if resets_at else None)
    return RateLimitEvent(rate_limit_info=info, uuid="u1", session_id=session_id)


class FakeQuery:
    """Plays one script per call: items are yielded; an exception item is raised."""

    def __init__(self, *scripts: list[Any]):
        self.scripts = list(scripts)
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions):
        self.calls.append((prompt, options))
        script = self.scripts[len(self.calls) - 1]

        async def gen():
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return gen()


def execute_full(make_api, agent_config, fake: FakeQuery, run: dict[str, Any] = NIGHTLY,
                 routes: dict[tuple[str, str], Any] | None = None, resume_session_id: str | None = None):
    api, router = make_api(routes or {})

    async def go():
        try:
            return await execute_run(run, config=agent_config, api=api, query_fn=fake, now=lambda: NOW,
                                     resume_session_id=resume_session_id)
        finally:
            await api.aclose()

    return asyncio.run(go()), router


def execute(*args: Any, **kwargs: Any):
    return execute_full(*args, **kwargs)[0]


# -- options ---------------------------------------------------------------------------


def test_options_isolated_and_subscription_only(make_api, agent_config, subscription_env):
    api, _ = make_api({})
    server = create_house_server(Toolkit(api))
    opts = build_options("nightly", agent_config, server)
    assert opts.tools == []  # no built-in tools at all
    assert set(DISALLOWED_TOOLS) <= set(opts.disallowed_tools)
    assert {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "NotebookEdit", "WebFetch", "WebSearch"} <= set(opts.disallowed_tools)
    assert opts.allowed_tools == [f"mcp__house__{t.name}" for t in TOOLS]
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert list(opts.mcp_servers) == ["house"]
    assert opts.max_turns == 30
    assert opts.model == "claude-opus-5-5" and opts.fallback_model == "claude-sonnet-5-5"
    assert opts.effort == "medium"
    assert opts.cwd == str(agent_config.cwd)
    assert "ANTHROPIC_API_KEY" not in opts.env and "CLAUDE_CODE_SIMPLE" not in opts.env
    assert not any("bare" in k for k in opts.extra_args)
    assert "Twins' Room" in opts.system_prompt and "# Nightly run" in opts.system_prompt
    assert build_options("weekly", agent_config, server).effort == "high"
    signin = build_options("signin_check", agent_config, None)
    assert signin.max_turns == 1 and signin.mcp_servers == {} and signin.allowed_tools == []
    assert signin.model == "claude-sonnet-5-5"


def test_executed_run_gets_empty_cwd_and_clean_env(make_api, agent_config, subscription_env, monkeypatch):
    fake = FakeQuery([init(), assistant("done"), result_message()])
    out = execute(make_api, agent_config, fake)
    assert out.status == "completed"
    prompt, opts = fake.calls[0]
    assert "Nightly run #42" in prompt
    cwd = agent_config.cwd
    assert cwd.is_dir() and list(cwd.iterdir()) == []
    assert "ANTHROPIC_API_KEY" not in opts.env


def test_prompts_for_each_kind():
    assert "The owner asks:\n\nIs the upstairs maxing out?" in user_prompt_for(
        {"id": 3, "kind": "chat", "prompt": "Is the upstairs maxing out?"}, NOW)
    trig = user_prompt_for({"id": 4, "kind": "triggered", "trigger": {"kind": "maxed", "unit_key": "up"}}, NOW)
    assert '"unit_key": "up"' in trig and "```json" in trig
    for kind in ("nightly", "weekly", "triggered", "chat"):
        assert system_prompt_for(kind).startswith("# Role")
    with pytest.raises(ValueError):
        user_prompt_for({"id": 5, "kind": "chat", "prompt": ""}, NOW)


# -- result mapping --------------------------------------------------------------------


def test_completed_run_maps_to_finish_body(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(), assistant("Working."), assistant("All done."), result_message()])
    out = execute(make_api, agent_config, fake)
    body = out.finish_body()
    assert body["status"] == "completed"
    assert body["result_text"] == "Nightly report published."
    assert body["terminal_reason"] == "completed" and body["num_turns"] == 9
    assert body["model"] == "claude-opus-5-5" and body["session_id"] == "sess-1"
    assert body["usage"]["input_tokens"] == 1200 and body["usage"]["duration_ms"] == 61000
    assert body["error"] is None and body["not_before"] is None
    assert out.signed_in is True


def test_result_without_completed_terminal_reason_is_not_trusted(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(), result_message(terminal_reason="max_turns", result="partial digest")])
    out = execute(make_api, agent_config, fake)
    assert out.status == "failed" and out.terminal_reason == "max_turns"
    assert out.result_text is None and "did not complete (max_turns)" in (out.error or "")

    fake = FakeQuery([init(), result_message(terminal_reason=None)])
    assert execute(make_api, agent_config, fake).status == "failed"

    fake = FakeQuery([init(), assistant("no result frame")])
    out = execute(make_api, agent_config, fake)
    assert out.status == "failed" and "without a result" in (out.error or "")


def test_usage_limit_defers_until_reset(make_api, agent_config, subscription_env):
    reset = NOW + timedelta(hours=3)
    fake = FakeQuery([init(), rate_limit("five_hour", reset), result_message(is_error=True, api_error_status=429), usage_limit_error()])
    out = execute(make_api, agent_config, fake)
    assert out.status == "deferred"
    assert out.not_before is not None and reset <= out.not_before <= reset + timedelta(minutes=2)
    assert out.finish_body()["not_before"].startswith("2026-10-04T11:3")
    assert out.signed_in is True and len(fake.calls) == 1  # not an Opus-only limit: no retry


def test_usage_limit_without_reset_info_defers_one_hour(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(), usage_limit_error()])
    out = execute(make_api, agent_config, fake)
    assert out.status == "deferred" and out.not_before == NOW + timedelta(hours=1)


def test_opus_only_limit_retries_once_on_fallback(make_api, agent_config, subscription_env):
    fake = FakeQuery(
        [init(), rate_limit("seven_day_opus", NOW + timedelta(days=2)), usage_limit_error("Opus weekly limit reached")],
        [init(), assistant("done", model="claude-sonnet-5-5"), result_message()],
    )
    out = execute(make_api, agent_config, fake)
    assert out.status == "completed" and out.model == "claude-sonnet-5-5"
    assert [opts.model for _, opts in fake.calls] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert fake.calls[1][1].fallback_model is None


def test_opus_limit_then_fallback_limit_defers(make_api, agent_config, subscription_env):
    reset = NOW + timedelta(hours=4)
    fake = FakeQuery(
        [init(), rate_limit("seven_day_opus", NOW + timedelta(days=2)), usage_limit_error()],
        [init(), rate_limit("five_hour", reset), usage_limit_error()],
    )
    out = execute(make_api, agent_config, fake)
    assert out.status == "deferred" and len(fake.calls) == 2
    assert out.not_before is not None and out.not_before >= reset


def test_other_result_error_fails(make_api, agent_config, subscription_env):
    err = ResultError("Claude Code returned an error result: overloaded",
                      data={"subtype": "success", "is_error": True, "api_error_status": 529,
                            "terminal_reason": "api_error", "result": "API Error: 529 overloaded"}, exit_code=1)
    out = execute(make_api, agent_config, FakeQuery([init(), err]))
    assert out.status == "failed" and out.terminal_reason == "api_error"
    assert "529 overloaded" in (out.error or "") and out.signed_in is None

    turns = ResultError("max turns", data={"subtype": "error_max_turns", "errors": ["Reached maximum number of turns (30)"],
                                           "terminal_reason": "max_turns"}, exit_code=1)
    out = execute(make_api, agent_config, FakeQuery([init(), turns]))
    assert out.status == "failed" and out.terminal_reason == "max_turns"


def test_sign_in_failure_is_reported(make_api, agent_config, subscription_env):
    # A normal run that fails sign-in is deferred 30 minutes (not failed) and flips signed_in.
    out = execute(make_api, agent_config, FakeQuery([init(), assistant("x", error="authentication_failed"), auth_error()]))
    assert out.status == "deferred" and out.signed_in is False
    assert out.not_before == NOW + timedelta(minutes=30)
    assert out.finish_body()["not_before"] == "2026-10-04T09:00:00+00:00"
    assert "claude setup-token" in (out.error or "") and "Deferred 30 min" in (out.error or "")
    # The sign-in check itself just reports it.
    out = execute(make_api, agent_config, FakeQuery([init(), auth_error()]), run={"id": None, "kind": "signin_check"})
    assert out.status == "failed" and out.signed_in is False and out.not_before is None


def test_bare_sdk_exception_fails_the_run_redacted(make_api, agent_config, subscription_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-1234567890-secret")
    # The SDK raises bare Exception for an initialize timeout or an error control_response.
    boom = Exception("Control request timeout: initialize (tok-1234567890-secret sk-ant-oat01-abcdefghijklmnop)")
    out = execute(make_api, agent_config, FakeQuery([init(session_id="sess-boom"), boom]))
    assert out.status == "failed" and out.signed_in is None
    error = out.error or ""
    assert "Exception" in error and "initialize" in error and "[redacted]" in error
    assert "tok-1234567890-secret" not in error and "abcdefghijkl" not in error
    assert out.session_id == "sess-boom" and out.model == "claude-opus-5-5"


def test_prompts_are_delivered_verbatim(make_api, agent_config, subscription_env):
    from dataclasses import replace

    api, _ = make_api({})
    opts = build_options("chat", agent_config, create_house_server(Toolkit(api)))
    assert opts.verbatim_prompts is True  # no @path expansion of the owner's text or trigger JSON
    assert build_options("signin_check", agent_config, None).verbatim_prompts is True
    with pytest.raises(SubscriptionGuardError, match="verbatim"):
        verify_options(replace(opts, verbatim_prompts=False))
    fake = FakeQuery([init(), result_message()])
    execute(make_api, agent_config, fake, run={"id": 8, "kind": "chat", "prompt": "What is in @/etc/passwd ?"})
    prompt, sent = fake.calls[0]
    assert sent.verbatim_prompts is True and "@/etc/passwd" in prompt
    asyncio.run(api.aclose())


# -- retries never repeat gated side effects ------------------------------------------

RUN_CREATED = "2026-10-04T08:00:00Z"
RUN = {**NIGHTLY, "created_at": RUN_CREATED}
PRIOR_WORK_ROUTES: dict[tuple[str, str], Any] = {
    ("GET", "/api/reports"): [
        {"id": 56, "kind": "note", "title": "Another run's note", "agent_run_id": 41, "created_at": "2026-10-04T08:10:00Z"},
        {"id": 55, "kind": "nightly", "title": "Nightly 10-03", "agent_run_id": 42, "created_at": "2026-10-04T08:20:00Z"},
    ],
    ("GET", "/api/changes"): [
        {**change(11, "owner", {"linked_offset_f": 1.5}, status="backtest"), "proposed_by": "claude",
         "created_at": "2026-10-04T08:21:00Z", "title": "Main 1.5F under"},
        {**change(7, "owner", {"linked_offset_f": 1.0}, status="held"), "decided_by": "claude",
         "decided_at": "2026-10-04T08:22:00Z"},
        {**change(3, "owner", {"linked_offset_f": 2.0}), "proposed_by": "claude", "created_at": "2026-10-01T08:00:00Z"},
    ],
    ("GET", "/api/experiments"): [
        {"id": 4, "name": "Offset switchback", "proposed_by": "claude", "created_at": "2026-10-04T08:23:00Z", "status": "proposed"},
        {"id": 2, "name": "Owner idea", "proposed_by": "owner", "created_at": "2026-10-04T08:24:00Z", "status": "proposed"},
    ],
}


def assert_prior_work_listed(prompt: str) -> None:
    assert "Retry note" in prompt and "Do NOT publish, propose or sign off any of these again" in prompt
    assert "published nightly report #55 'Nightly 10-03'" in prompt
    assert "proposed change #11" in prompt and "signed off change #7" in prompt and "proposed experiment #4" in prompt
    # another run's report, a proposal from before this run was queued, the owner's experiment: not listed
    assert "#56" not in prompt and "change #3" not in prompt and "experiment #2" not in prompt


def opus_limited_then_ok() -> FakeQuery:
    return FakeQuery(
        [init(session_id="sess-9"), rate_limit("seven_day_opus", NOW + timedelta(days=2)), usage_limit_error("Opus weekly limit reached")],
        [init(), assistant("done", model="claude-sonnet-5-5"), result_message()],
    )


def test_fallback_retry_resumes_the_session_and_lists_prior_work(make_api, agent_config, subscription_env, monkeypatch):
    monkeypatch.setattr(runner_mod, "session_exists", lambda session_id, cwd: session_id == "sess-9")
    fake = opus_limited_then_ok()
    out = execute(make_api, agent_config, fake, run=RUN, routes=PRIOR_WORK_ROUTES)
    assert out.status == "completed" and len(fake.calls) == 2
    (first_prompt, first), (retry_prompt, retry) = fake.calls
    assert first.resume is None and "Retry note" not in first_prompt  # a fresh run is not a retry
    assert retry.resume == "sess-9" and retry.model == "claude-sonnet-5-5"
    assert "resumed" in retry_prompt and "Opus usage limit" in retry_prompt
    assert "Nightly run #42" not in retry_prompt  # the original task is already in the resumed session
    assert_prior_work_listed(retry_prompt)


def test_fallback_retry_without_the_session_on_disk_reruns_with_the_note(make_api, agent_config, subscription_env):
    fake = opus_limited_then_ok()
    execute(make_api, agent_config, fake, run=RUN, routes=PRIOR_WORK_ROUTES)
    retry_prompt, retry = fake.calls[1]
    assert retry.resume is None and retry_prompt.startswith("Nightly run #42")
    assert_prior_work_listed(retry_prompt)


def test_reclaimed_deferred_run_resumes_and_gets_the_note(make_api, agent_config, subscription_env, monkeypatch):
    deferred = {**RUN, "not_before": "2026-10-04T08:25:00Z", "terminal_reason": "api_error", "model": "claude-opus-5-5"}
    # Session still on disk: resumed, with the note.
    monkeypatch.setattr(runner_mod, "session_exists", lambda session_id, cwd: True)
    fake = FakeQuery([init(), result_message()])
    execute(make_api, agent_config, fake, run=deferred, routes=PRIOR_WORK_ROUTES, resume_session_id="sess-7")
    prompt, opts = fake.calls[0]
    assert opts.resume == "sess-7" and "resumed" in prompt
    assert_prior_work_listed(prompt)
    # Session unknown (the API's run carries no session id): a fresh run, still with the note.
    monkeypatch.setattr(runner_mod, "session_exists", lambda session_id, cwd: False)
    fake = FakeQuery([init(), result_message()])
    execute(make_api, agent_config, fake, run=deferred, routes=PRIOR_WORK_ROUTES)
    prompt, opts = fake.calls[0]
    assert opts.resume is None and prompt.startswith("Nightly run #42")
    assert_prior_work_listed(prompt)
    # API down: the note says what could not be checked.
    fake = FakeQuery([init(), result_message()])
    execute(make_api, agent_config, fake, run=deferred)
    assert "Could not check reports, changes, experiments" in fake.calls[0][0]


def test_fresh_run_makes_no_prior_work_calls(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(), result_message()])
    _, router = execute_full(make_api, agent_config, fake, run=RUN, routes=PRIOR_WORK_ROUTES)
    assert router.requests == []
    assert "Retry note" not in fake.calls[0][0] and fake.calls[0][1].resume is None


def test_api_key_billing_aborts_the_run(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(api_key_source="ANTHROPIC_API_KEY"), assistant("should never get here"), result_message()])
    out = execute(make_api, agent_config, fake)
    assert out.status == "failed" and "apiKeySource=ANTHROPIC_API_KEY" in (out.error or "")


def test_refuses_when_api_key_in_environment(make_api, agent_config, subscription_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-secretsecretsecret")
    fake = FakeQuery([init(), result_message()])
    out = execute(make_api, agent_config, fake)
    assert out.status == "failed" and "ANTHROPIC_API_KEY" in (out.error or "")
    assert "secretsecret" not in (out.error or "")
    assert fake.calls == []  # Claude was never started


def test_refuses_non_empty_cwd(make_api, agent_config, subscription_env):
    agent_config.cwd.mkdir(parents=True)
    (agent_config.cwd / "secrets.env").write_text("nope")
    fake = FakeQuery([init(), result_message()])
    out = execute(make_api, agent_config, fake)
    assert out.status == "failed" and "not empty" in (out.error or "") and fake.calls == []


def test_timeout_fails_the_run(make_api, agent_config, subscription_env):
    from dataclasses import replace

    class Slow(FakeQuery):
        def __call__(self, *, prompt: str, options: ClaudeAgentOptions):
            self.calls.append((prompt, options))

            async def gen():
                yield init()
                await asyncio.sleep(10)
                yield result_message()

            return gen()

    out = execute(make_api, replace(agent_config, run_timeout_s=0.05), Slow())
    assert out.status == "failed" and out.terminal_reason == "timeout"


def test_run_one_returns_finish_dict(make_api, agent_config, subscription_env):
    api, _ = make_api({})
    fake = FakeQuery([init(), result_message(result="Answer: upstairs maxed 45 min.")])

    async def go():
        try:
            return await run_one({"id": 7, "kind": "chat", "prompt": "How long did upstairs max out?"}, fake,
                                 config=agent_config, api=api)
        finally:
            await api.aclose()

    body = asyncio.run(go())
    assert body["status"] == "completed" and body["result_text"] == "Answer: upstairs maxed 45 min."
    assert set(body) == {"status", "result_text", "model", "terminal_reason", "num_turns", "usage", "error",
                         "session_id", "not_before"}

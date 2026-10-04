"""Scheduler: startup refusals (API key, --bare, missing token), token-expiry warnings, the
sign-in check, heartbeats, and claim -> run -> finish. Claude is a fake ``query``."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from test_runner import FakeQuery, auth_error, init, rate_limit, result_message, usage_limit_error

from climate_agent import runner as runner_mod
from climate_agent import scheduler as scheduler_mod
from climate_agent.config import AgentConfig, StartupRefused, add_one_year, check_startup, idle_reason
from climate_agent.scheduler import Scheduler

NOW = datetime(2026, 10, 4, 8, 30, tzinfo=UTC)
GOOD_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth", "CLIMATE_AGENT_TOKEN": "agent-token"}


# -- startup ---------------------------------------------------------------------------


def test_refuses_to_start_with_api_key():
    env = {**GOOD_ENV, "ANTHROPIC_API_KEY": "sk-ant-api03-zzzzzzzzzzzz"}
    with pytest.raises(StartupRefused) as exc:
        check_startup(env)
    assert "ANTHROPIC_API_KEY is set" in str(exc.value) and "bill the API" in str(exc.value)
    assert "zzzzzzzz" not in str(exc.value)


@pytest.mark.parametrize("name", ["ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"])
def test_refuses_other_billing_routes(name):
    with pytest.raises(StartupRefused):
        check_startup({**GOOD_ENV, name: "1"})


def test_refuses_bare_mode_but_idles_on_missing_tokens():
    with pytest.raises(StartupRefused, match="--bare"):
        check_startup({**GOOD_ENV, "CLAUDE_CODE_SIMPLE": "1"})
    # Missing tokens don't exit (the default-on service would restart in a loop): it idles.
    assert "CLAUDE_CODE_OAUTH_TOKEN is not set" in idle_reason(check_startup({"CLIMATE_AGENT_TOKEN": "agent-token"}))
    missing = idle_reason(check_startup({"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth"})) or ""
    assert "CLIMATE_AGENT_TOKEN is not set" in missing and "More → Security → API tokens" in missing
    broken = idle_reason(check_startup({"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth", "CLIMATE_AGENT_TOKEN": "cai_1_ab\ncd"}))
    assert broken is not None and "malformed" in broken
    pasted = check_startup({"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth", "CLIMATE_AGENT_TOKEN": " Bearer cai_1f_secret "})
    assert pasted.agent_token == "cai_1f_secret" and idle_reason(pasted) is None
    assert idle_reason(check_startup(dict(GOOD_ENV))) is None


def test_main_idles_without_oauth_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLIMATE_AGENT_TOKEN", "agent-token")
    seen: dict[str, str] = {}

    async def fake_idle(config: AgentConfig, reason: str) -> bool:
        seen["reason"] = reason
        return True

    async def must_not_run(config: AgentConfig) -> bool:
        raise AssertionError("Claude must not run without a sign-in token")

    monkeypatch.setattr(scheduler_mod, "_aidle", fake_idle)
    monkeypatch.setattr(scheduler_mod, "_amain", must_not_run)
    assert scheduler_mod.main([]) == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN" in seen["reason"]


def test_empty_api_key_is_removed_not_inherited():
    env = {**GOOD_ENV, "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_SIMPLE": "0"}
    config = check_startup(env)
    assert "ANTHROPIC_API_KEY" not in env
    assert config.model == "claude-opus-5-5" and config.max_turns == 30


def test_main_exits_before_running_when_api_key_set(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-zzzzzzzzzzzz")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth")
    monkeypatch.setenv("CLIMATE_AGENT_TOKEN", "agent-token")

    async def must_not_run(config: AgentConfig) -> bool:
        raise AssertionError("the agent loop must not start")

    monkeypatch.setattr(scheduler_mod, "_amain", must_not_run)
    with caplog.at_level(logging.ERROR):
        assert scheduler_mod.main([]) == 2
    assert "refusing to start" in caplog.text
    assert "zzzzzzzz" not in caplog.text and "fake-oauth" not in caplog.text


def test_config_repr_has_no_secrets():
    config = AgentConfig.from_env({**GOOD_ENV, "CLIMATE_AGENT_TOKEN": "very-secret-agent-token"})
    assert "very-secret" not in repr(config) and "fake-oauth" not in repr(config)


# -- token expiry ----------------------------------------------------------------------


def test_token_expiry_warning():
    config = AgentConfig.from_env({**GOOD_ENV, "CLAUDE_TOKEN_CREATED": "2025-11-01"})
    assert config.token_expires_at == date(2026, 11, 1)
    assert config.token_warning(date(2026, 9, 1)) is None
    warning = config.token_warning(date(2026, 10, 10))
    assert warning is not None and "2026-11-01" in warning and "22 days" in warning and "claude setup-token" in warning
    assert "expired" in (config.token_warning(date(2026, 11, 2)) or "")
    assert AgentConfig.from_env(GOOD_ENV).token_warning(date(2026, 10, 10)) is None
    assert add_one_year(date(2028, 2, 29)) == date(2029, 2, 28)


# -- the loop --------------------------------------------------------------------------


def make_scheduler(make_api, agent_config, routes: dict[tuple[str, str], Any], fake: FakeQuery):
    api, router = make_api(routes)
    return Scheduler(agent_config, api, query_fn=fake, now=lambda: NOW), router, api


def test_heartbeat_reports_versions_and_expiry(make_api, agent_config, subscription_env):
    from dataclasses import replace

    config = replace(agent_config, token_created=date(2025, 10, 20))
    sched, router, api = make_scheduler(make_api, config, {("POST", "/api/agent/heartbeat"): {"enabled": True}}, FakeQuery())
    sched.signed_in = True

    async def go():
        try:
            return await sched.heartbeat()
        finally:
            await api.aclose()

    assert asyncio.run(go()) is True
    body = router.bodies("POST", "/api/agent/heartbeat")[0]
    assert body["signed_in"] is True and body["token_expires_at"] == "2026-10-20"
    assert body["sdk_version"] == "0.2.163" and body["cli_version"]
    assert "16 days" in body["detail"]["token_warning"]
    assert "Authorization" not in str(body)


def test_signin_check_once_per_start(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(), result_message(result="OK", num_turns=1)])
    sched, _, api = make_scheduler(make_api, agent_config, {}, fake)

    async def go():
        try:
            first = await sched.signin_check()
            second = await sched.signin_check()
            return first, second
        finally:
            await api.aclose()

    assert asyncio.run(go()) == (True, True)
    assert len(fake.calls) == 1
    prompt, opts = fake.calls[0]
    assert opts.max_turns == 1 and opts.mcp_servers == {} and "OK" in prompt


def test_signin_check_usage_limited_still_signed_in(make_api, agent_config, subscription_env):
    fake = FakeQuery([init(), usage_limit_error()])
    sched, _, api = make_scheduler(make_api, agent_config, {}, fake)

    async def go():
        try:
            return await sched.signin_check()
        finally:
            await api.aclose()

    assert asyncio.run(go()) is True


def test_claim_run_finish(make_api, agent_config, subscription_env):
    run = {"id": 42, "kind": "nightly", "status": "running", "requested_by": "schedule", "created_at": "2026-10-04T08:30:00Z"}
    routes = {
        ("POST", "/api/agent/claim"): run,
        ("POST", "/api/agent/runs/42/finish"): {**run, "status": "completed"},
    }
    fake = FakeQuery([init(), result_message()])
    sched, router, api = make_scheduler(make_api, agent_config, routes, fake)

    async def go():
        try:
            return await sched.claim_once()
        finally:
            await api.aclose()

    assert asyncio.run(go()) is True
    finish = router.bodies("POST", "/api/agent/runs/42/finish")
    assert len(finish) == 1 and finish[0]["status"] == "completed"
    assert finish[0]["result_text"] == "Nightly report published."
    assert sched.current_run_id is None and sched.signed_in is True


def test_claim_nothing_queued(make_api, agent_config, subscription_env):
    fake = FakeQuery()
    sched, router, api = make_scheduler(make_api, agent_config, {("POST", "/api/agent/claim"): None}, fake)

    async def go():
        try:
            return await sched.claim_once()
        finally:
            await api.aclose()

    assert asyncio.run(go()) is False
    assert router.paths() == ["POST /api/agent/claim"] and fake.calls == []


def test_deferred_run_reports_not_before(make_api, agent_config, subscription_env):
    run = {"id": 43, "kind": "weekly"}
    routes = {("POST", "/api/agent/claim"): run, ("POST", "/api/agent/runs/43/finish"): {}}
    fake = FakeQuery([init(), usage_limit_error()])
    sched, router, api = make_scheduler(make_api, agent_config, routes, fake)

    async def go():
        try:
            return await sched.claim_once()
        finally:
            await api.aclose()

    asyncio.run(go())
    body = router.bodies("POST", "/api/agent/runs/43/finish")[0]
    assert body["status"] == "deferred" and body["not_before"] == "2026-10-04T09:30:00+00:00"


def test_stop_mid_run_hands_the_run_back(make_api, agent_config, subscription_env):
    run = {"id": 44, "kind": "nightly"}
    routes = {("POST", "/api/agent/claim"): run, ("POST", "/api/agent/runs/44/finish"): {},
              ("POST", "/api/agent/heartbeat"): {}}

    class Hang(FakeQuery):
        def __call__(self, *, prompt, options):
            self.calls.append((prompt, options))

            async def gen():
                yield init()
                await asyncio.sleep(30)
                yield result_message()

            return gen()

    sched, router, api = make_scheduler(make_api, agent_config, routes, Hang())
    sched.signin_checked = True

    async def go():
        try:
            task = asyncio.create_task(sched.run())
            await asyncio.sleep(0.2)
            sched.stop()
            return await asyncio.wait_for(task, timeout=5)
        finally:
            await api.aclose()

    assert asyncio.run(go()) is True
    body = router.bodies("POST", "/api/agent/runs/44/finish")[0]
    assert body["status"] == "deferred" and body["terminal_reason"] == "service_stopped"


def test_claimed_run_is_finished_even_when_running_it_crashes(make_api, agent_config, subscription_env, monkeypatch):
    run = {"id": 47, "kind": "nightly"}
    routes = {("POST", "/api/agent/claim"): run, ("POST", "/api/agent/runs/47/finish"): {}}
    sched, router, api = make_scheduler(make_api, agent_config, routes, FakeQuery())

    async def crash(*args: Any, **kwargs: Any):
        raise RuntimeError("unexpected sk-ant-oat01-zzzzzzzzzzzzzzzz")

    monkeypatch.setattr(scheduler_mod, "execute_run", crash)

    async def go():
        try:
            return await sched.claim_once()
        finally:
            await api.aclose()

    assert asyncio.run(go()) is True
    body = router.bodies("POST", "/api/agent/runs/47/finish")[0]
    assert body["status"] == "failed" and body["terminal_reason"] == "agent_error"
    assert "RuntimeError" in body["error"] and "zzzzzzzz" not in body["error"]
    assert sched.current_run_id is None


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def test_signed_out_claims_nothing_and_rechecks_every_30_minutes(make_api, agent_config, subscription_env):
    run = {"id": 48, "kind": "nightly"}
    routes = {("POST", "/api/agent/claim"): run, ("POST", "/api/agent/runs/48/finish"): {},
              ("POST", "/api/agent/heartbeat"): {}}
    fake = FakeQuery([init(), result_message(result="OK", num_turns=1)], [init(), result_message()])
    api, router = make_api(routes)
    clock = Clock(NOW)
    sched = Scheduler(agent_config, api, query_fn=fake, now=clock)
    sched.signin_checked, sched.signed_in, sched.signin_checked_at = True, False, NOW

    async def go():
        try:
            steps = [await sched.work_once()]
            clock.at = NOW + timedelta(minutes=29)
            steps.append(await sched.work_once())
            assert router.requests == [] and fake.calls == []  # signed out: nothing claimed, no Claude run
            clock.at = NOW + timedelta(minutes=30)
            steps.append(await sched.work_once())  # the sign-in re-check (one tiny run)
            steps.append(await sched.work_once())  # signed in again: claims
            return steps
        finally:
            await api.aclose()

    assert asyncio.run(go()) == [False, False, True, True]
    assert fake.calls[0][1].max_turns == 1 and fake.calls[0][1].mcp_servers == {}
    assert router.paths() == ["POST /api/agent/heartbeat", "POST /api/agent/claim", "POST /api/agent/runs/48/finish"]
    assert router.bodies("POST", "/api/agent/heartbeat")[0]["signed_in"] is True
    assert router.bodies("POST", "/api/agent/runs/48/finish")[0]["status"] == "completed"


def test_failed_signin_check_does_not_claim(make_api, agent_config, subscription_env):
    routes = {("POST", "/api/agent/claim"): {"id": 49, "kind": "nightly"}, ("POST", "/api/agent/heartbeat"): {}}
    fake = FakeQuery([init(), auth_error()])
    sched, router, api = make_scheduler(make_api, agent_config, routes, fake)

    async def go():
        try:
            await sched.signin_check()
            return await sched.work_once()
        finally:
            await api.aclose()

    assert asyncio.run(go()) is False
    assert sched.signed_in is False and len(fake.calls) == 1
    assert "POST /api/agent/claim" not in router.paths()


def test_auth_failure_mid_run_defers_it_and_stops_claiming(make_api, agent_config, subscription_env):
    run = {"id": 45, "kind": "nightly"}
    routes = {("POST", "/api/agent/claim"): run, ("POST", "/api/agent/runs/45/finish"): {},
              ("POST", "/api/agent/heartbeat"): {}}
    fake = FakeQuery([init(), auth_error()])
    sched, router, api = make_scheduler(make_api, agent_config, routes, fake)
    sched.signin_checked, sched.signed_in = True, True

    async def go():
        try:
            return [await sched.work_once(), await sched.work_once()]
        finally:
            await api.aclose()

    assert asyncio.run(go()) == [True, False]
    body = router.bodies("POST", "/api/agent/runs/45/finish")[0]
    assert body["status"] == "deferred" and body["not_before"] == "2026-10-04T09:00:00+00:00"
    assert "claude setup-token" in body["error"]
    assert sched.signed_in is False and sched.signin_checked_at == NOW
    assert router.bodies("POST", "/api/agent/heartbeat")[0]["signed_in"] is False  # reported right away
    assert router.paths().count("POST /api/agent/claim") == 1  # nothing more is claimed while signed out


def test_reclaimed_deferred_run_resumes_its_session(make_api, agent_config, subscription_env, monkeypatch):
    monkeypatch.setattr(runner_mod, "session_exists", lambda session_id, cwd: session_id == "sess-46")
    first = {"id": 46, "kind": "weekly", "created_at": "2026-10-04T08:00:00Z"}
    again = {**first, "not_before": "2026-10-04T11:31:00Z", "terminal_reason": "api_error", "model": "claude-opus-5-5"}
    claims = [first, again]
    routes = {
        ("POST", "/api/agent/claim"): lambda request: claims.pop(0),
        ("POST", "/api/agent/runs/46/finish"): {},
        ("GET", "/api/reports"): [], ("GET", "/api/changes"): [], ("GET", "/api/experiments"): [],
    }
    fake = FakeQuery(
        [init(session_id="sess-46"), rate_limit("five_hour", NOW + timedelta(hours=3), "sess-46"),
         usage_limit_error(session_id="sess-46")],
        [init(), result_message()],
    )
    sched, router, api = make_scheduler(make_api, agent_config, routes, fake)

    async def go():
        try:
            await sched.claim_once()
            await sched.claim_once()
        finally:
            await api.aclose()

    asyncio.run(go())
    assert [b["status"] for b in router.bodies("POST", "/api/agent/runs/46/finish")] == ["deferred", "completed"]
    (_, opts1), (prompt2, opts2) = fake.calls
    assert opts1.resume is None and opts2.resume == "sess-46"
    assert "resumed" in prompt2 and "nothing needs skipping" in prompt2


# -- the API token ---------------------------------------------------------------------


def _check(make_api, route: Any) -> bool | None:
    api, _ = make_api({("GET", "/api/auth/me"): route})

    async def go():
        try:
            return await scheduler_mod.check_api_access(api)
        finally:
            await api.aclose()

    return asyncio.run(go())


def test_startup_token_check(make_api, caplog):
    import httpx

    with caplog.at_level(logging.INFO, logger="climate_agent.scheduler"):
        assert _check(make_api, {"role": "agent", "token_id": 3, "token_name": "agent on the server"}) is True
    assert "API token accepted (agent role, app token 'agent on the server')" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="climate_agent.scheduler"):
        revoked = httpx.Response(401, json={"detail": {"code": "NOT_AUTHENTICATED", "message": "This API token has been revoked."}})
        assert _check(make_api, revoked) is False
        assert _check(make_api, {"role": "viewer", "token_id": 4, "token_name": "wall tablet"}) is False
    assert "revoked" in caplog.text and "viewer role" in caplog.text
    assert caplog.text.count("More → Security → API tokens") == 2 and "test-agent-token" not in caplog.text
    assert _check(make_api, httpx.Response(404, json={"detail": "Not Found"})) is None  # an older API


def test_refused_token_logged_once_not_every_retry(make_api, agent_config, subscription_env, caplog):
    import httpx

    clock = {"now": NOW}
    refused = httpx.Response(401, json={"detail": {"code": "NOT_AUTHENTICATED", "message": "Unknown API token."}})
    api, _ = make_api({("POST", "/api/agent/claim"): refused, ("POST", "/api/agent/heartbeat"): refused})
    sched = Scheduler(agent_config, api, query_fn=FakeQuery(), now=lambda: clock["now"])
    sched.signed_in = True

    async def go(n: int):
        for _ in range(n):
            assert await sched.claim_once() is False
            assert await sched.heartbeat() is False
            clock["now"] += timedelta(seconds=20)

    with caplog.at_level(logging.WARNING, logger="climate_agent.scheduler"):
        asyncio.run(go(10))
        assert caplog.text.count("Unknown API token") == 1 and "refused" in caplog.text
        clock["now"] += scheduler_mod.AUTH_RELOG
        asyncio.run(go(1))
        assert caplog.text.count("Unknown API token") == 2  # reminded after AUTH_RELOG
    asyncio.run(api.aclose())


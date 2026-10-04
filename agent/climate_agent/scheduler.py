"""``python -m climate_agent.scheduler``: the agent service's main loop.

- Refuses to start if anything would bill the API instead of the owner's subscription
  (``ANTHROPIC_API_KEY`` and friends, ``--bare`` mode). Without a sign-in token it idles and
  heartbeats "not signed in" (no restart loop; nothing is claimed).
- Checks sign-in once per container start with a one-turn run, and reports it.
- Heartbeats every 60 s (``POST /agent/heartbeat``): sign-in state, SDK/CLI versions, token
  expiry (the API raises the 30-day warning from it).
- Claims a queued run every 20 s (``POST /agent/claim``), runs it, posts ``/finish``. One run
  at a time. Claude is never in the control path, so a stopped agent only delays reports.
  Every claimed run is finished, even when running it raises unexpectedly (as failed).
- Signed out (a failed sign-in check, or a run that failed sign-in, which defers that run 30
  minutes): claims nothing, keeps heartbeating "not signed in", re-checks sign-in at most
  every 30 minutes with one tiny run, and resumes claiming once it works.
- A run deferred by this process is resumed from its Claude session when claimed again.
- SIGTERM/SIGINT: stop claiming; a run in flight is cancelled and handed back as deferred so
  the next start picks it up.
- API token: any token the API accepts with the agent role works (a ``cai_...`` agent token
  made in the app, or the legacy ``CLIMATE_AGENT_TOKEN``); nothing here looks at its format.
  At start it asks the API who it is (``GET /auth/me``). A refused token (401, or a 403 from
  the auth layer) is logged as an error that says how to fix it, when it first appears and
  then at most every 30 minutes, not on every 20-second retry.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import query

from climate_agent import __version__
from climate_agent.api import RESTART_HELP, TOKEN_HELP, ApiClient, ApiError
from climate_agent.config import AgentConfig, ConfigError, StartupRefused, check_startup, idle_reason
from climate_agent.runner import QueryFn, RunOutcome, execute_run, redact

log = logging.getLogger("climate_agent.scheduler")

FINISH_ATTEMPTS = 4
STOP_FINISH_TIMEOUT_S = 8.0
SIGNIN_RECHECK = timedelta(minutes=30)
MAX_REMEMBERED_SESSIONS = 50
AUTH_RELOG = timedelta(minutes=30)


def cli_version() -> str | None:
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:
        return None
    return str(__cli_version__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


class ApiFailureLog:
    """Logs failed API calls. A refused token is an ERROR with the fix spelled out (see
    ``ApiError.auth_hint``), logged when it first appears or changes and then at most every
    AUTH_RELOG, because the loops retry every 20-60 s; anything else stays a warning."""

    def __init__(self, now: Callable[[], datetime] = _utcnow):
        self.now = now
        self.problem: str | None = None
        self._logged_at: datetime | None = None

    def failed(self, what: str, exc: ApiError) -> None:
        hint = exc.auth_hint
        if hint is None:
            log.warning("%s failed: %s", what, exc)
            return
        now = self.now()
        if hint != self.problem or self._logged_at is None or now - self._logged_at >= AUTH_RELOG:
            log.error("%s refused: %s. %s", what, exc, hint)
            self._logged_at = now
        self.problem = hint

    def ok(self) -> None:
        """A call went through: the token works again, so a new refusal is logged at once."""
        self.problem = None
        self._logged_at = None


async def check_api_access(api: ApiClient) -> bool | None:
    """Ask the API who this token is (``GET /auth/me``) once at start and log the answer.

    True: accepted with the agent role. False: refused, or a token with another role (a viewer
    or control token cannot claim runs); logged as an error with the fix. None: unknown (the
    API is not reachable yet, or predates ``/auth/me``); the loops report problems as they go."""
    try:
        me = await api.get("/auth/me")
    except ApiError as exc:
        if exc.auth_hint:
            log.error("the API refused the agent's token: %s. %s", exc, exc.auth_hint)
            return False
        if exc.status_code is None:
            log.warning("could not check the API token yet: %s", exc)
        return None
    role = me.get("role") if isinstance(me, dict) else None
    if role is None:
        return None
    if role != "agent":
        log.error(
            "the token in %s has the %s role; the agent needs a token with the agent role. %s, %s.",
            api.token_name, role, TOKEN_HELP, RESTART_HELP,
        )
        return False
    name = me.get("token_name")
    log.info("API token accepted (agent role%s)", f", app token {name!r}" if name else "")
    return True


class Scheduler:
    def __init__(
        self,
        config: AgentConfig,
        api: ApiClient,
        *,
        query_fn: QueryFn = query,
        now: Callable[[], datetime] = _utcnow,
    ):
        self.config = config
        self.api = api
        self.query_fn = query_fn
        self.now = now
        self.signed_in: bool | None = None
        self.signin_checked = False
        self.signin_checked_at: datetime | None = None
        self.current_run_id: int | None = None
        # run id -> Claude session id of runs this process deferred, to resume on reclaim.
        self._run_sessions: dict[int, str] = {}
        self.last_error: str | None = None
        self.api_log = ApiFailureLog(now)
        self._stop = asyncio.Event()
        self._warned_on: date | None = None

    def stop(self) -> None:
        if not self._stop.is_set():
            log.info("stop requested")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- sign-in ---------------------------------------------------------------------

    def signin_recheck_due(self) -> bool:
        """Signed out, and the last sign-in check is at least SIGNIN_RECHECK old."""
        if self.signed_in is not False:
            return False
        return self.signin_checked_at is None or self.now() - self.signin_checked_at >= SIGNIN_RECHECK

    async def signin_check(self) -> bool | None:
        """One tiny run per process start and, while signed out, again at most every 30
        minutes. Usage-limited counts as signed in."""
        if self.signin_checked and not self.signin_recheck_due():
            return self.signed_in
        self.signin_checked = True
        self.signin_checked_at = self.now()
        try:
            outcome = await execute_run(
                {"id": None, "kind": "signin_check"}, config=self.config, api=self.api, query_fn=self.query_fn, now=self.now
            )
        except Exception as exc:  # noqa: BLE001 - a crash here must not end the work loop
            outcome = RunOutcome(status="failed", error=redact(f"Sign-in check crashed: {type(exc).__name__}: {exc}"))
        if outcome.signed_in is not None:
            self.signed_in = outcome.signed_in
        elif outcome.status == "completed":
            self.signed_in = True
        if self.signed_in:
            self.last_error = None
            log.info("Claude sign-in OK (subscription)")
        else:
            self.last_error = outcome.error
            log.error("Claude sign-in check failed: %s", outcome.error)
        return self.signed_in

    # -- heartbeat ---------------------------------------------------------------------

    def token_warning(self) -> str | None:
        return self.config.token_warning(self.now().date())

    def heartbeat_body(self) -> dict[str, Any]:
        expires = self.config.token_expires_at
        return {
            "signed_in": self.signed_in,
            "sdk_version": claude_agent_sdk.__version__,
            "cli_version": cli_version(),
            "token_expires_at": expires.isoformat() if expires else None,
            "detail": {
                "agent_version": __version__,
                "model": self.config.model,
                "fallback_model": self.config.fallback_model,
                "busy_run_id": self.current_run_id,
                "token_warning": self.token_warning(),
                "token_created_known": self.config.token_created is not None,
                "last_error": self.last_error,
            },
        }

    async def heartbeat(self) -> bool:
        warning = self.token_warning()
        today = self.now().date()
        if warning and self._warned_on != today:
            log.warning(warning)
            self._warned_on = today
        try:
            await self.api.post("/agent/heartbeat", self.heartbeat_body())
        except ApiError as exc:
            self.api_log.failed("heartbeat", exc)
            return False
        self.api_log.ok()
        return True

    async def heartbeat_loop(self) -> None:
        while not self.stopping:
            await self.heartbeat()
            await _wait(self._stop, self.config.heartbeat_s)

    # -- runs --------------------------------------------------------------------------

    async def finish(self, run_id: int, body: Mapping[str, Any]) -> bool:
        delay = 2.0
        for attempt in range(1, FINISH_ATTEMPTS + 1):
            try:
                await self.api.post(f"/agent/runs/{run_id}/finish", dict(body))
                return True
            except ApiError as exc:
                if exc.is_client_error or attempt == FINISH_ATTEMPTS:
                    hint = f" {exc.auth_hint}" if exc.auth_hint else ""
                    log.error("could not report run %s as %s: %s.%s", run_id, body.get("status"), exc, hint)
                    return False
                log.warning("finish for run %s failed (%s); retrying in %.0f s", run_id, exc, delay)
                await asyncio.sleep(delay)
                delay *= 2
        return False

    async def claim_once(self) -> bool:
        """Claim and run one queued run. Returns True when a run was processed."""
        try:
            run = await self.api.post("/agent/claim")
        except ApiError as exc:
            self.api_log.failed("claim", exc)
            return False
        self.api_log.ok()
        if not run:
            return False
        run_id = int(run["id"])
        self.current_run_id = run_id
        log.info("running %s run %s", run.get("kind"), run_id)
        resume = self._run_sessions.pop(run_id, None)
        try:
            outcome = await execute_run(
                run, config=self.config, api=self.api, query_fn=self.query_fn, now=self.now, resume_session_id=resume
            )
        except asyncio.CancelledError:
            handback = RunOutcome(
                status="deferred",
                error="The agent service stopped during this run; it will be retried.",
                terminal_reason="service_stopped",
                not_before=self.now() + timedelta(minutes=1),
            )
            try:
                await asyncio.wait_for(self.finish(run_id, handback.finish_body()), timeout=STOP_FINISH_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                log.error("could not hand run %s back before stopping", run_id)
            raise
        except Exception as exc:  # noqa: BLE001 - a claimed run is always finished
            log.error("run %s crashed: %s", run_id, redact(repr(exc)))
            outcome = RunOutcome(
                status="failed",
                terminal_reason="agent_error",
                error=redact(f"The agent service hit an unexpected error during this run: {type(exc).__name__}: {exc}"),
            )
        finally:
            self.current_run_id = None
        if outcome.signed_in is not None:
            self.signed_in = outcome.signed_in
            if outcome.signed_in is False:
                self.signin_checked_at = self.now()  # this run just checked; re-check in 30 min
        if outcome.status == "deferred" and outcome.session_id:
            self._remember_session(run_id, outcome.session_id)
        self.last_error = outcome.error if outcome.status != "completed" else None
        log.info("run %s %s%s", run_id, outcome.status, f": {outcome.error}" if outcome.error else "")
        await self.finish(run_id, outcome.finish_body())
        return True

    def _remember_session(self, run_id: int, session_id: str) -> None:
        self._run_sessions[run_id] = session_id
        while len(self._run_sessions) > MAX_REMEMBERED_SESSIONS:
            self._run_sessions.pop(next(iter(self._run_sessions)))

    async def work_once(self) -> bool:
        """One pass of the work loop. Returns True when it did something (no wait needed).

        Signed out: claim nothing (each run would fail sign-in too); re-check sign-in at most
        every SIGNIN_RECHECK and resume claiming once it works."""
        if self.signed_in is False:
            if not self.signin_recheck_due():
                return False
            await self.signin_check()
            await self.heartbeat()  # report the result right away
            return self.signed_in is not False
        before = self.signed_in
        processed = await self.claim_once()
        if self.signed_in != before:
            await self.heartbeat()
        return processed

    async def work_loop(self) -> None:
        await self.signin_check()
        await self.heartbeat()  # report the sign-in result right away
        while not self.stopping:
            if not await self.work_once():
                await _wait(self._stop, self.config.claim_s)

    async def run(self) -> bool:
        """Run until ``stop()``. Returns False if the work loop crashed."""
        heartbeat = asyncio.create_task(self.heartbeat_loop(), name="heartbeat")
        work = asyncio.create_task(self.work_loop(), name="work")
        stopper = asyncio.create_task(self._stop.wait(), name="stop")
        await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in (work, heartbeat, stopper):
            task.cancel()
        results = await asyncio.gather(work, heartbeat, stopper, return_exceptions=True)
        ok = True
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                log.error("agent loop ended with an error: %s", redact(repr(result)))
                ok = False
        return ok


async def _amain(config: AgentConfig) -> bool:
    api = ApiClient(config.api_url, config.agent_token, timeout_s=config.api_timeout_s)
    await check_api_access(api)
    scheduler = Scheduler(config, api)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, scheduler.stop)
    try:
        return await scheduler.run()
    finally:
        await api.aclose()


async def _aidle(config: AgentConfig, reason: str) -> bool:
    """No usable token: never run Claude; heartbeat 'not signed in' so the app shows why."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        api: ApiClient | None = ApiClient(config.api_url, config.agent_token, timeout_s=config.api_timeout_s)
    except ValueError:  # no token, or one that cannot be sent (idle_reason says which)
        api = None
    api_log = ApiFailureLog()
    try:
        while not stop.is_set():
            if api is not None:
                body = {
                    "signed_in": False,
                    "sdk_version": claude_agent_sdk.__version__,
                    "cli_version": cli_version(),
                    "token_expires_at": None,
                    "detail": {"agent_version": __version__, "idle": True, "last_error": reason},
                }
                try:
                    await api.post("/agent/heartbeat", body)
                    api_log.ok()
                except ApiError as exc:
                    api_log.failed("heartbeat", exc)
            await _wait(stop, config.heartbeat_s)
    finally:
        if api is not None:
            await api.aclose()
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = check_startup()
    except ConfigError as exc:
        log.error("bad configuration: %s", exc)
        return 2
    except StartupRefused as exc:
        log.error("refusing to start: %s", exc)
        return 2
    if config.log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        logging.getLogger().setLevel(config.log_level)
    reason = idle_reason(config)
    if reason:
        log.warning("idle, Claude will not run: %s", reason)
        return 0 if asyncio.run(_aidle(config, reason)) else 1
    log.info(
        "climate agent %s (claude-agent-sdk %s, CLI %s): %r",
        __version__, claude_agent_sdk.__version__, cli_version(), config,
    )
    warning = config.token_warning(datetime.now(UTC).date())
    if warning:
        log.warning(warning)
    if config.token_created is None:
        log.warning("CLAUDE_TOKEN_CREATED is not set; token expiry cannot be tracked or warned about.")
    return 0 if asyncio.run(_amain(config)) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

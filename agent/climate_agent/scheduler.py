"""``python -m climate_agent.scheduler``: the agent service's main loop.

- Refuses to start if anything would bill the API instead of the owner's subscription
  (``ANTHROPIC_API_KEY`` and friends, ``--bare`` mode) or if the sign-in token is missing.
- Checks sign-in once per container start with a one-turn run, and reports it.
- Heartbeats every 60 s (``POST /agent/heartbeat``): sign-in state, SDK/CLI versions, token
  expiry (the API raises the 30-day warning from it).
- Claims a queued run every 20 s (``POST /agent/claim``), runs it, posts ``/finish``. One run
  at a time. Claude is never in the control path, so a stopped agent only delays reports.
- SIGTERM/SIGINT: stop claiming; a run in flight is cancelled and handed back as deferred so
  the next start picks it up.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import query

from climate_agent import __version__
from climate_agent.api import ApiClient, ApiError
from climate_agent.config import AgentConfig, ConfigError, StartupRefused, check_startup
from climate_agent.runner import QueryFn, RunOutcome, execute_run, redact

log = logging.getLogger("climate_agent.scheduler")

FINISH_ATTEMPTS = 4
STOP_FINISH_TIMEOUT_S = 8.0


def cli_version() -> str | None:
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:
        return None
    return str(__cli_version__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


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
        self.current_run_id: int | None = None
        self.last_error: str | None = None
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

    async def signin_check(self) -> bool | None:
        """One tiny run per process start. Usage-limited counts as signed in."""
        if self.signin_checked:
            return self.signed_in
        self.signin_checked = True
        outcome = await execute_run(
            {"id": None, "kind": "signin_check"}, config=self.config, api=self.api, query_fn=self.query_fn, now=self.now
        )
        if outcome.signed_in is not None:
            self.signed_in = outcome.signed_in
        elif outcome.status == "completed":
            self.signed_in = True
        if self.signed_in:
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
            log.warning("heartbeat failed: %s", exc)
            return False
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
                    log.error("could not report run %s as %s: %s", run_id, body.get("status"), exc)
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
            log.warning("claim failed: %s", exc)
            return False
        if not run:
            return False
        run_id = int(run["id"])
        self.current_run_id = run_id
        log.info("running %s run %s", run.get("kind"), run_id)
        try:
            outcome = await execute_run(run, config=self.config, api=self.api, query_fn=self.query_fn, now=self.now)
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
        finally:
            self.current_run_id = None
        if outcome.signed_in is not None:
            self.signed_in = outcome.signed_in
        self.last_error = outcome.error if outcome.status != "completed" else None
        log.info("run %s %s%s", run_id, outcome.status, f": {outcome.error}" if outcome.error else "")
        await self.finish(run_id, outcome.finish_body())
        return True

    async def work_loop(self) -> None:
        await self.signin_check()
        await self.heartbeat()  # report the sign-in result right away
        while not self.stopping:
            processed = await self.claim_once()
            if not processed:
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
    scheduler = Scheduler(config, api)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, scheduler.stop)
    try:
        return await scheduler.run()
    finally:
        await api.aclose()


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
    log.info(
        "climate agent %s (claude-agent-sdk %s, CLI %s): %r",
        __version__, claude_agent_sdk.__version__, cli_version(), config,
    )
    warning = config.token_warning(date.today())
    if warning:
        log.warning(warning)
    if config.token_created is None:
        log.warning("CLAUDE_TOKEN_CREATED is not set; token expiry cannot be tracked or warned about.")
    return 0 if asyncio.run(_amain(config)) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

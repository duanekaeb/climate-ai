"""The worker process: ``python -m climate.worker``.

Owns the ThermostatSource (simulator or ecobee) and runs, with independent cadences:
poll (summary/detail), runtime pulls, weather sync (hourly), controller tick (every 3 min),
queued owner actions (every 10 s), change gates (every 15 min), nightly jobs (baselines,
room offsets, RC fit, experiment days, daily report, agent schedule), the jobs table (refit /
backtest / backfill requests), anomaly triggers for Claude (drift, sensor offline, upstairs
maxed out), and a heartbeat. Switching app_settings['source'] swaps the source live.

Every loop is wrapped: a failure is logged (redacted) and retried with backoff; it never
kills the process or the other loops. Other modules are imported when a loop runs, so an
unfinished or broken module only stalls its own loop. Every call into the source goes
through one lock (``LockedSource``): ecobee wants one request at a time, and two concurrent
token refreshes would race each other for the rotating refresh token.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import signal
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update

from climate import agent_queue, notify
from climate.collector import backfill as backfill_mod
from climate.collector.backfill import jsonable, run_db_step
from climate.collector.poller import (
    merge_beat,
    poll_once,
    pull_runtime,
    record_source_failure,
    redact,
    safe_error,
    source_state,
)
from climate.config import Settings, get_settings
from climate.sources.base import ThermostatSource
from climate.store.app_settings import (
    AgentSettings,
    LocationSettings,
    SourceSettings,
    get_raw,
    get_setting,
    put_setting,
)
from climate.store.db import session_scope
from climate.store.orm import Job, LiveSensor, Reading5m, Sensor
from climate.timeutil import local_date, parse_hhmm, to_local, utcnow

log = logging.getLogger("climate.worker")

SOURCE_LOOP_S = 5  # checks the source setting; polls when the poll interval has elapsed
REBUILD_RETRY_S = 60
RUNTIME_PULL_S = 3600
WEATHER_S = 3600
TICK_S = 180
SIM_TICK_MIN_S = 60
EXECUTE_S = 10
CHANGES_S = 15 * 60
DAILY_CHECK_S = 60
SCHEDULE_S = 60
JOBS_S = 10
ANOMALY_S = 15 * 60
HEARTBEAT_S = 30
MAX_BACKOFF_S = 300
NIGHTLY_RUNTIME_AT = "02:10"
NIGHTLY_JOBS_AT = "02:30"
SENSOR_OFFLINE_AFTER = timedelta(minutes=30)
SENSOR_SILENT_AFTER = timedelta(hours=3)  # no data at all (ecobee runtime lags up to ~2 h)
UPSTAIRS_MAXED_TRIGGER_MIN = 60.0
DAILY_KEY = "worker:daily"
ANOMALY_KEY = "worker:anomaly"


def configure_logging(level: str) -> None:
    lvl = logging.getLevelName(level.upper())
    lvl = lvl if isinstance(lvl, int) else logging.INFO
    logging.basicConfig(level=lvl, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # pyecobee logs tokens at DEBUG; httpx/httpcore/urllib3 log request URLs, and ecobee's token
    # endpoint carries the refresh token in the query string.
    logging.getLogger("pyecobee").setLevel(max(logging.INFO, lvl))
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(max(logging.WARNING, lvl))


def _trace(exc: BaseException) -> str:
    return redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-4000:]


class LockedSource:
    """Serializes every awaited call into the wrapped source (see the module docstring)."""

    def __init__(self, inner: ThermostatSource, lock: asyncio.Lock | None = None) -> None:
        self._inner = inner
        self._lock = lock or asyncio.Lock()
        self.kind = inner.kind

    @property
    def inner(self) -> ThermostatSource:
        return self._inner

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def poll_revisions(self) -> Any:
        async with self._lock:
            return await self._inner.poll_revisions()

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> Any:
        async with self._lock:
            return await self._inner.fetch_snapshots(unit_keys)

    async def fetch_runtime(self, start: datetime, end: datetime) -> Any:
        async with self._lock:
            return await self._inner.fetch_runtime(start, end)

    async def set_hold(self, req: Any) -> Any:
        async with self._lock:
            return await self._inner.set_hold(req)

    async def resume_program(self, unit_key: str, reason: str) -> Any:
        async with self._lock:
            return await self._inner.resume_program(unit_key, reason)

    async def health(self) -> Any:
        async with self._lock:
            return await self._inner.health()

    async def close(self) -> None:
        async with self._lock:
            await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if inspect.iscoroutinefunction(attr):
            async def locked(*args: Any, **kwargs: Any) -> Any:
                async with self._lock:
                    return await attr(*args, **kwargs)

            return locked
        return attr


@dataclass
class _LoopState:
    failures: int = 0
    last_error: str | None = None


# ---------------------------------------------------------------------------------------
# sync work (runs in threads)
# ---------------------------------------------------------------------------------------


def _tz() -> str:
    with session_scope() as s:
        return get_setting(s, "location", LocationSettings).tz


def _optional(module: str) -> Any | None:
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module:
            return None
        raise


def run_fits(now: datetime) -> dict[str, Any]:
    """The nightly model fits (also the 'refit' job): baselines, room offsets, RC (shadow),
    occupancy priors when that module exists. Each step in its own transaction."""
    steps: dict[str, Any] = {
        "baselines": run_db_step("baseline refit", lambda s: importlib.import_module("climate.analytics.baseline").refit_all(s, now)),
        "room_offsets": run_db_step("room offsets", lambda s: importlib.import_module("climate.models.thermal_rc").fit_room_offsets(s, now)),
        "rc": run_db_step("rc fit", lambda s: importlib.import_module("climate.models.thermal_rc").fit_rc(s, now)),
    }
    priors = _optional("climate.occupancy.priors")
    if priors is not None and hasattr(priors, "fit_priors"):
        steps["occupancy_priors"] = run_db_step("occupancy priors", lambda s: priors.fit_priors(s, now))
    return steps


def build_report(day: date) -> dict[str, Any]:
    return run_db_step(
        "daily report", lambda s: {"report_id": importlib.import_module("climate.analytics.reports").build_daily_report(s, day),
                                   "date": day.isoformat()},
    )


def run_nightly(now: datetime) -> dict[str, Any]:
    """02:30 local: fits, experiment days, yesterday's daily report."""
    out = run_fits(now)
    out["experiment_days"] = run_db_step(
        "experiment days", lambda s: importlib.import_module("climate.experiments.analysis").update_days(s, now)
    )
    out["daily_report"] = build_report(local_date(now, _tz()) - timedelta(days=1))
    return out


def _claim_job(now: datetime) -> tuple[int, str, dict[str, Any]] | None:
    with session_scope() as s:
        job = s.execute(
            select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1).with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if job is None:
            return None
        job.status = "running"
        job.started_at = now
        return job.id, job.kind, dict(job.params or {})


def _finish_job(job_id: int, status: str, result: dict[str, Any] | None, error: str | None) -> None:
    with session_scope() as s:
        s.execute(
            update(Job).where(Job.id == job_id)
            .values(status=status, result=jsonable(result) if result is not None else None, error=error, finished_at=utcnow())
        )


def _fail_orphaned_jobs() -> int:
    """Jobs left 'running' by a worker that died cannot be resumed; say so."""
    with session_scope() as s:
        res = s.execute(
            update(Job).where(Job.status == "running")
            .values(status="failed", error="The worker restarted while this job was running.", finished_at=func.now())
        )
        return res.rowcount or 0


def _load_json(key: str) -> dict[str, Any]:
    with session_scope() as s:
        raw = get_raw(s, key)
    return dict(raw) if isinstance(raw, dict) else {}


def _store_json(key: str, value: dict[str, Any]) -> None:
    with session_scope() as s:
        put_setting(s, key, value, updated_by="worker")


def check_sensors(now: datetime) -> list[dict[str, Any]]:
    """Raise 'sensor_offline:<key>' for a sensor offline (or silent) too long; resolve it when
    the sensor reports again. Returns trigger events for alerts raised just now.

    Offline = the source reports it offline and its last good temperature is > 30 min old,
    or no data at all for 3 h. Sensors that never reported (not mapped yet) are ignored."""
    events: list[dict[str, Any]] = []
    with session_scope() as s:
        sensors = list(s.execute(select(Sensor.key, Sensor.name).where(Sensor.is_active).order_by(Sensor.sort)))
        live = {r.sensor_key: r for r in s.execute(select(LiveSensor)).scalars()}
        last_good = dict(
            s.execute(
                select(Reading5m.sensor_key, func.max(Reading5m.ts))
                .where(Reading5m.temp_f.is_not(None), Reading5m.ts >= now - timedelta(days=2))
                .group_by(Reading5m.sensor_key)
            ).all()
        )
        for key, name in sensors:
            lv = live.get(key)
            good = [t for t in (last_good.get(key), lv.ts if lv is not None and lv.online and lv.temp_f is not None else None) if t]
            if lv is None and not good:
                continue
            newest_good = max(good) if good else None
            flagged = lv is not None and lv.online is False
            if flagged:
                offline = newest_good is None or now - newest_good > SENSOR_OFFLINE_AFTER
            else:
                newest_any = max([t for t in (newest_good, lv.ts if lv is not None else None) if t])
                offline = now - newest_any > SENSOR_SILENT_AFTER
            dedupe = f"sensor_offline:{key}"
            if not offline:
                notify.resolve_alert(s, dedupe)
                continue
            since = newest_good.isoformat() if newest_good else "unknown"
            alert_id = notify.raise_alert(
                s, "sensor_offline", "warn", f"{name} sensor offline",
                f"No reading from {name} since {since}. Its room counts as occupied (uncertain) until it reports again.",
                dedupe_key=dedupe,
            )
            if alert_id is not None:
                events.append({"kind": "sensor_offline", "sensor_key": key, "since": since, "alert_id": alert_id})
    return events


def detect_anomalies(now: datetime) -> list[dict[str, Any]]:
    """Drift, sensor offline and upstairs-maxed checks. Each check is independent."""
    tz = _tz()
    events: list[dict[str, Any]] = []
    try:
        with session_scope() as s:
            report = importlib.import_module("climate.analytics.metrics").drift(s)
        for u in report.units:
            if u.drifting:
                events.append({"kind": "drift", "key": f"drift:{u.unit_key}:{u.mode}", "unit_key": u.unit_key,
                               "mode": u.mode, "z": round(float(u.z), 2), "resid_mean_pct": round(float(u.resid_mean_pct), 1)})
    except Exception as exc:  # noqa: BLE001
        log.info("drift check skipped: %s", safe_error(exc))
    try:
        events.extend(check_sensors(now))
    except Exception as exc:  # noqa: BLE001
        log.warning("sensor check failed: %s", safe_error(exc))
    try:
        with session_scope() as s:
            today = importlib.import_module("climate.analytics.metrics").unit_today(s, now, tz)
        maxed = float((today.get("up") or {}).get("maxed_minutes_today") or 0.0)
        if maxed > UPSTAIRS_MAXED_TRIGGER_MIN:
            events.append({"kind": "upstairs_maxed", "key": "maxed:up", "unit_key": "up", "maxed_minutes_today": round(maxed)})
    except Exception as exc:  # noqa: BLE001
        log.info("maxed-out check skipped: %s", safe_error(exc))
    return events


def trigger_agent(now: datetime, events: list[dict[str, Any]]) -> list[int]:
    """Queue (or coalesce into) a triggered Claude run for each NEW anomaly. Drift and
    maxed-out fire at most once per local day per key; sensor alerts fire once per outage."""
    if not events:
        return []
    with session_scope() as s:
        if not get_setting(s, "agent", AgentSettings).enabled:
            return []
    today = local_date(now, _tz()).isoformat()
    state = _load_json(ANOMALY_KEY)
    fired: set[str] = set(state.get("keys") or []) if state.get("date") == today else set()
    run_ids: list[int] = []
    for ev in events:
        key = ev.get("key")
        if key and key in fired:
            continue
        trigger = {k: v for k, v in ev.items() if k != "key"}
        trigger["at"] = now.isoformat()
        try:
            with session_scope() as s:
                run = agent_queue.enqueue(s, "triggered", "anomaly", prompt="", trigger=trigger, now=now)
                run_ids.append(run.id)
        except agent_queue.TriggerLimitReached as exc:
            log.info("anomaly %s not sent to Claude: %s", ev.get("kind"), exc)
        if key:
            fired.add(key)
    _store_json(ANOMALY_KEY, {"date": today, "keys": sorted(fired)})
    return sorted(set(run_ids))


def _schedule_agent(now: datetime) -> list[int]:
    with session_scope() as s:
        return agent_queue.schedule_due(s, now)


def _advance_changes(now: datetime) -> Any:
    with session_scope() as s:
        return importlib.import_module("climate.control.changes").advance(s, now)


def _prepare_db() -> None:
    if os.environ.get("CLIMATE_MIGRATE_ON_START", "1") == "1":
        from climate.house import seed
        from climate.store.migrate import migrate

        migrate()
        with session_scope() as s:
            seed(s)
    else:
        with session_scope() as s:
            s.execute(text("SELECT 1"))


# ---------------------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------------------


class Worker:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or get_settings()
        self.source: LockedSource | None = None
        self.source_kind: str | None = None
        self.wanted_kind: str | None = None
        self.revisions: dict[str, str] = {}
        self.last_poll_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self._last_poll_mono: float | None = None
        self._last_build_mono: float | None = None
        self._loops: dict[str, _LoopState] = {}
        self._daily: dict[str, str] = {}
        self._stop = asyncio.Event()

    # --- lifecycle --------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        if not await self._wait_for_db():
            return
        await self._startup()
        tasks = [
            asyncio.create_task(self._loop(name, interval, body), name=f"worker:{name}")
            for name, interval, body in self._schedule()
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_source()
            log.info("worker stopped")

    async def _wait_for_db(self) -> bool:
        delay = 2.0
        while not self.stopping:
            try:
                await asyncio.to_thread(_prepare_db)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("database not ready (%s); retrying in %.0f s", safe_error(exc), delay)
                if await self._sleep(delay):
                    return False
                delay = min(60.0, delay * 2)
        return False

    async def _startup(self) -> None:
        try:
            n = await asyncio.to_thread(_fail_orphaned_jobs)
            if n:
                log.warning("%d job(s) were left running by a previous worker; marked failed", n)
            self._daily = {k: str(v) for k, v in (await asyncio.to_thread(_load_json, DAILY_KEY)).items()}
        except Exception as exc:  # noqa: BLE001
            log.warning("startup bookkeeping failed: %s", safe_error(exc))
        # First start in simulator mode: generate history BEFORE building the live simulator,
        # so it restores the state the history ended in and the series continue seamlessly.
        try:
            wanted = await asyncio.to_thread(self._wanted_kind)
        except Exception as exc:  # noqa: BLE001
            wanted = None
            log.warning("could not read the source setting: %s", safe_error(exc))
        if wanted == "simulator" and self.cfg.sim_backfill_days > 0:
            try:
                result = await backfill_mod.backfill_simulator_if_empty(self.cfg.sim_backfill_days)
                if result:
                    log.info("simulator history ready: %d runtime rows", result.get("runtime_rows", 0))
            except Exception as exc:  # noqa: BLE001
                log.warning("simulator history backfill failed:\n%s", _trace(exc))
        await self.ensure_source()

    async def _close_source(self) -> None:
        src, self.source, self.source_kind = self.source, None, None
        self.revisions = {}
        if src is None:
            return
        try:
            await asyncio.wait_for(src.close(), timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.warning("closing the source failed: %s", safe_error(exc))

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless stopping; True when the worker is stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            return False
        return True

    async def _loop(self, name: str, interval: Callable[[], float], body: Callable[[], Awaitable[Any]]) -> None:
        st = self._loops.setdefault(name, _LoopState())
        while not self.stopping:
            try:
                await body()
                if st.failures:
                    log.info("%s recovered after %d failure(s)", name, st.failures)
                st.failures, st.last_error = 0, None
                delay = interval()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one loop's failure never kills the process
                st.failures += 1
                err = safe_error(exc)
                if err != st.last_error:
                    log.warning("%s failed:\n%s", name, _trace(exc))
                else:
                    log.warning("%s failed again (%d in a row): %s", name, st.failures, err)
                st.last_error = err
                delay = min(MAX_BACKOFF_S, 10 * 2 ** min(st.failures - 1, 5))
            if await self._sleep(delay):
                return

    def _schedule(self) -> list[tuple[str, Callable[[], float], Callable[[], Awaitable[Any]]]]:
        return [
            ("source", lambda: SOURCE_LOOP_S, self.source_step),
            ("runtime", lambda: RUNTIME_PULL_S, self.runtime_step),
            ("daily", lambda: DAILY_CHECK_S, self.daily_step),
            ("weather", lambda: WEATHER_S, self.weather_step),
            ("tick", self.tick_interval, self.tick_step),
            ("execute", lambda: EXECUTE_S, self.execute_step),
            ("changes", lambda: CHANGES_S, self.changes_step),
            ("agent_schedule", lambda: SCHEDULE_S, self.schedule_step),
            ("jobs", lambda: JOBS_S, self.jobs_step),
            ("anomalies", lambda: ANOMALY_S, self.anomaly_step),
            ("heartbeat", lambda: HEARTBEAT_S, self.heartbeat_step),
        ]

    # --- source -----------------------------------------------------------------------

    def _wanted_kind(self) -> str:
        with session_scope() as s:
            return get_setting(s, "source", SourceSettings).kind

    async def ensure_source(self) -> LockedSource | None:
        """Build the configured source; rebuild when the setting changes. A failed build (e.g.
        ecobee not signed in yet) is retried every REBUILD_RETRY_S."""
        kind = self.wanted_kind = self._wanted_kind()
        if self.source is not None and kind != self.source_kind:
            log.info("switching data source %s -> %s", self.source_kind, kind)
            await self._close_source()
            self._last_poll_mono = None
            self._last_build_mono = None
        if self.source is not None:
            return self.source
        mono = time.monotonic()
        if self._last_build_mono is not None and mono - self._last_build_mono < REBUILD_RETRY_S:
            return None
        self._last_build_mono = mono
        try:
            from climate.sources import get_source

            inner = get_source(kind)
        except Exception as exc:  # noqa: BLE001 - e.g. ecobee not signed in yet
            await asyncio.to_thread(record_source_failure, kind, exc, utcnow())
            return None
        self.source, self.source_kind, self.revisions = LockedSource(inner), kind, {}
        log.info("data source: %s", kind)
        return self.source

    def poll_interval(self) -> float:
        return float(self.cfg.poll_seconds if self.source_kind == "ecobee" else self.cfg.sim_poll_seconds)

    def tick_interval(self) -> float:
        if self.source_kind == "simulator":
            return float(max(SIM_TICK_MIN_S, self.cfg.sim_poll_seconds))
        return float(TICK_S)

    async def source_step(self) -> None:
        src = await self.ensure_source()
        if src is None:
            return
        mono = time.monotonic()
        if self._last_poll_mono is not None and mono - self._last_poll_mono < self.poll_interval():
            return
        self._last_poll_mono = mono
        now = utcnow()
        if src.kind == "simulator":
            advance = getattr(src.inner, "advance_to", None)
            if advance is not None:
                async with src.lock:
                    await asyncio.to_thread(advance, now)
        self.revisions = await poll_once(src, now, self.revisions)
        self.last_poll_at = now
        if source_state(src.kind).auth_failed:
            # Drop the signed-out adapter; the rebuild (after REBUILD_RETRY_S, then at the poll
            # cadence) picks up the refresh token the owner stores by signing in again in Setup.
            await self._close_source()
            self._last_build_mono = time.monotonic()
            return
        if src.kind == "simulator":
            await pull_runtime(src, now, hours_back=1)

    async def runtime_step(self) -> None:
        if self.source is not None:
            await pull_runtime(self.source, utcnow(), hours_back=2)

    def _due_today(self, name: str, hhmm: str, now: datetime, tz: str) -> date | None:
        local_now = to_local(now, tz)
        if local_now.time() < parse_hhmm(hhmm) or self._daily.get(name) == local_now.date().isoformat():
            return None
        return local_now.date()

    async def _mark_done(self, name: str, day: date) -> None:
        self._daily[name] = day.isoformat()
        await asyncio.to_thread(_store_json, DAILY_KEY, dict(self._daily))

    async def daily_step(self) -> None:
        now = utcnow()
        tz = await asyncio.to_thread(_tz)
        day = self._due_today("runtime_nightly", NIGHTLY_RUNTIME_AT, now, tz)
        if day is not None and self.source is not None:
            try:
                await pull_runtime(self.source, now, hours_back=48)
            finally:
                await self._mark_done("runtime_nightly", day)
        day = self._due_today("nightly", NIGHTLY_JOBS_AT, now, tz)
        if day is not None:
            try:
                result = await asyncio.to_thread(run_nightly, now)
                failed = [k for k, v in result.items() if not v.get("ok")]
                log.info("nightly jobs done%s", f" (failed: {', '.join(failed)})" if failed else "")
            finally:
                await self._mark_done("nightly", day)

    async def weather_step(self) -> None:
        from climate.collector import weather

        await weather.sync_weather(utcnow())

    async def tick_step(self) -> None:
        from climate.control import controller

        now = utcnow()
        await controller.tick(self.source, now)
        self.last_tick_at = now

    async def execute_step(self) -> None:
        from climate.control import controller

        await controller.execute_queued(self.source, utcnow())

    async def changes_step(self) -> None:
        await asyncio.to_thread(_advance_changes, utcnow())

    async def schedule_step(self) -> None:
        ids = await asyncio.to_thread(_schedule_agent, utcnow())
        if ids:
            log.info("queued scheduled Claude run(s) %s", ids)

    async def anomaly_step(self) -> None:
        now = utcnow()
        events = await asyncio.to_thread(detect_anomalies, now)
        ids = await asyncio.to_thread(trigger_agent, now, events)
        if ids:
            log.info("anomalies %s -> Claude run(s) %s", [e["kind"] for e in events], ids)

    async def heartbeat_step(self) -> None:
        def _beat() -> None:
            with session_scope() as s:
                merge_beat(s, "worker", source=self.source_kind or self.wanted_kind, last_poll_at=self.last_poll_at,
                           last_tick_at=self.last_tick_at)

        await asyncio.to_thread(_beat)

    # --- jobs ---------------------------------------------------------------------------

    async def jobs_step(self) -> None:
        await self.process_jobs()

    async def process_jobs(self, now: datetime | None = None, limit: int = 5) -> list[int]:
        """Run queued jobs (oldest first, up to ``limit``): mark running, then done/failed."""
        handled: list[int] = []
        for _ in range(limit):
            claimed = await asyncio.to_thread(_claim_job, now or utcnow())
            if claimed is None:
                break
            job_id, kind, params = claimed
            log.info("job %d (%s) started", job_id, kind)
            try:
                result = await self.run_job(kind, params, now or utcnow())
            except Exception as exc:  # noqa: BLE001 - recorded on the job row
                log.warning("job %d (%s) failed: %s", job_id, kind, safe_error(exc))
                await asyncio.to_thread(_finish_job, job_id, "failed", None, safe_error(exc, 2000))
            else:
                await asyncio.to_thread(_finish_job, job_id, "done", result, None)
                log.info("job %d (%s) done", job_id, kind)
            handled.append(job_id)
        return handled

    async def run_job(self, kind: str, params: dict[str, Any], now: datetime) -> dict[str, Any]:
        if kind == "refit":
            steps = await asyncio.to_thread(run_fits, now)
            if steps and not any(v.get("ok") for v in steps.values()):
                raise RuntimeError("every fit failed: " + "; ".join(f"{k}: {v.get('error')}" for k, v in steps.items()))
            return {"steps": steps}
        if kind == "backfill":
            days = max(1, min(3650, int(params.get("days", 30))))
            src = self.source or await self.ensure_source()
            if src is None:
                raise RuntimeError("no data source is available (check Setup)")
            return await backfill_mod.backfill(src, now - timedelta(days=days), now)
        if kind == "report":
            raw = params.get("date") or params.get("day")
            day = date.fromisoformat(str(raw)) if raw else local_date(now, await asyncio.to_thread(_tz)) - timedelta(days=1)
            out = await asyncio.to_thread(build_report, day)
            if not out.get("ok"):
                raise RuntimeError(str(out.get("error")))
            return out["result"]
        raise ValueError(f"unknown job kind {kind!r}")


def main() -> None:
    cfg = get_settings()
    configure_logging(cfg.log_level)
    log.info("climate worker %s starting", cfg.version)
    asyncio.run(Worker(cfg).run())


if __name__ == "__main__":
    main()

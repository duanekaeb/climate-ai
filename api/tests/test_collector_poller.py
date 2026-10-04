"""collector.poller with a fake source: change detection, failures, alerts, circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from climate.collector import poller
from climate.sources.base import RuntimeInterval, SensorReading, UnitSnapshot
from climate.sources.ecobee import EcobeeAuthError
from climate.store.app_settings import SourceSettings, beat, get_heartbeat, get_setting, put_setting
from climate.store.orm import Alert, LiveUnit, Runtime5m

NOW = datetime(2026, 7, 1, 18, 0, tzinfo=UTC)


class FakeSource:
    kind = "ecobee"

    def __init__(self) -> None:
        self.revs = {"main": "r1", "up": "r1", "bed": "r1"}
        self.fetched: list[list[str] | None] = []
        self.runtime_calls: list[tuple[datetime, datetime]] = []
        self.fail: Exception | None = None

    async def poll_revisions(self) -> dict[str, str]:
        if self.fail:
            raise self.fail
        return dict(self.revs)

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        self.fetched.append(unit_keys)
        keys = unit_keys if unit_keys is not None else list(self.revs)
        return [
            UnitSnapshot(unit_key=k, ts=NOW, source="ecobee", revision=self.revs[k], zone_temp_f=75.0,
                         sensors=[SensorReading(sensor_key=f"{k}.x", ts=NOW, temp_f=70.0)])
            for k in keys
        ]

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        self.runtime_calls.append((start, end))
        return [RuntimeInterval(unit_key="up", ts=start + timedelta(minutes=5 * i), comp_cool1=300) for i in range(3)]


@pytest.fixture(autouse=True)
def _fresh_state():
    poller.reset_states()
    yield
    poller.reset_states()


def open_alert(db, key: str) -> Alert | None:
    db.expire_all()
    return db.execute(select(Alert).where(Alert.dedupe_key == key, Alert.resolved_at.is_(None))).scalar_one_or_none()


async def test_detail_fetch_only_for_changed_revisions(db):
    src = FakeSource()
    revs = await poller.poll_once(src, NOW, {})
    assert src.fetched == [None] and revs == src.revs

    revs = await poller.poll_once(src, NOW, revs)
    assert src.fetched == [None]  # nothing changed: summary only

    src.revs["up"] = "r2"
    revs = await poller.poll_once(src, NOW, revs)
    assert src.fetched[-1] == ["up"] and revs["up"] == "r2"

    db.expire_all()
    assert db.get(LiveUnit, "up").revision == "r2"
    hb = get_heartbeat(db, "worker")
    assert hb.detail["source_ok"] is True and hb.detail["source"] == "ecobee"
    assert hb.detail["last_poll_at"] == NOW.isoformat()


async def test_three_failures_alert_and_open_the_circuit(db):
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True))
    beat(db, "worker", source="ecobee", last_tick_at="keep-me")
    db.commit()
    src = FakeSource()
    revs = await poller.poll_once(src, NOW, {})

    src.fail = TimeoutError("ecobee timed out")
    for i in range(2):
        assert await poller.poll_once(src, NOW + timedelta(minutes=3 * (i + 1)), revs) == revs
    assert open_alert(db, "source_down") is None
    db.expire_all()
    assert get_setting(db, "source", SourceSettings).cloud_circuit_open_until is None

    t3 = NOW + timedelta(minutes=9)
    await poller.poll_once(src, t3, revs)
    alert = open_alert(db, "source_down")
    assert alert is not None and alert.level == "warn" and "TimeoutError" in alert.body
    db.expire_all()
    assert get_setting(db, "source", SourceSettings).cloud_circuit_open_until == t3 + timedelta(minutes=15)
    hb = get_heartbeat(db, "worker")
    assert hb.detail["source_ok"] is False and hb.detail["source_consecutive_failures"] == 3
    assert hb.detail["last_tick_at"] == "keep-me"  # merged, not replaced

    src.fail = None
    await poller.poll_once(src, t3 + timedelta(minutes=3), revs)
    assert open_alert(db, "source_down") is None
    db.expire_all()
    settings = get_setting(db, "source", SourceSettings)
    assert settings.cloud_circuit_open_until is None and settings.homekit_enabled is True
    assert poller.source_state("ecobee").consecutive_failures == 0


async def test_no_circuit_without_homekit(db):
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=False))
    db.commit()
    src = FakeSource()
    src.fail = ConnectionError("down")
    for i in range(3):
        await poller.poll_once(src, NOW + timedelta(minutes=i), {})
    assert open_alert(db, "source_down") is not None
    db.expire_all()
    assert get_setting(db, "source", SourceSettings).cloud_circuit_open_until is None


async def test_auth_error_alerts_at_once(db):
    src = FakeSource()
    src.fail = EcobeeAuthError("refresh failed for https://api.ecobee.com/token?grant_type=refresh_token&code=SECRET123")
    await poller.poll_once(src, NOW, {})
    alert = open_alert(db, "ecobee_auth")
    assert alert is not None and alert.level == "error" and alert.title == "Sign in to ecobee again in Setup"
    assert poller.source_state("ecobee").auth_failed
    db.expire_all()
    assert "SECRET123" not in str(get_heartbeat(db, "worker").detail)

    src.fail = None
    await poller.poll_once(src, NOW + timedelta(minutes=3), {})
    assert open_alert(db, "ecobee_auth") is None
    assert not poller.source_state("ecobee").auth_failed


def test_redaction():
    exc = RuntimeError("POST https://api.ecobee.com/token?grant_type=refresh_token&code=abc.def failed; "
                       "Authorization: Bearer eyJhbGci.x.y; refresh_token=zzz")
    text = poller.safe_error(exc)
    for secret in ("abc.def", "eyJhbGci", "zzz"):
        assert secret not in text
    assert text.startswith("RuntimeError: POST https://api.ecobee.com/token?")


async def test_pull_runtime(db):
    src = FakeSource()
    n = await poller.pull_runtime(src, NOW + timedelta(minutes=7), hours_back=2)
    assert n == 3
    start, end = src.runtime_calls[0]
    assert start == datetime(2026, 7, 1, 16, 5, tzinfo=UTC) and end == NOW + timedelta(minutes=7)
    db.expire_all()
    rows = db.execute(select(Runtime5m)).scalars().all()
    assert {r.source for r in rows} == {"ecobee_report"}

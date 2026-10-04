"""Keeping ecobee's Smart Away and Follow Me off (controller.keep_settings, run by the worker at
start and once a day), and LockedSource passing an owner's ``force`` through."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from climate import worker as wmod
from climate.control import controller
from climate.sources.base import WriteResult
from climate.store.app_settings import ControlSettings, put_setting
from tests.test_controller_tick import NOW, actions, put_snapshot

OFF = {"autoAway": False, "followMeComfort": False}


class FakeEcobee:
    kind = "ecobee"

    def __init__(self, results: list[WriteResult] | None = None) -> None:
        self.calls = 0
        self.results = results if results is not None else [
            WriteResult(ok=True, channel="ecobee", before=OFF, readback=OFF,
                        request={"unit_key": "main", "settings": OFF, "noop": True}),
            WriteResult(ok=True, channel="ecobee", before={"autoAway": True, "followMeComfort": False}, readback=OFF,
                        request={"unit_key": "up", "identifier": "222", "settings": OFF}),
            WriteResult(ok=False, channel="ecobee", before={"autoAway": True, "followMeComfort": True},
                        readback={"autoAway": True, "followMeComfort": False}, request={"unit_key": "bed", "settings": OFF},
                        error="settings did not read back as autoAway=false, followMeComfort=false"),
        ]

    async def ensure_settings(self) -> list[WriteResult]:
        self.calls += 1
        return self.results


def set_mode(db, mode: str, act_units: list[str] | None = None) -> None:
    ctl = ControlSettings(mode=mode)  # type: ignore[arg-type]
    if act_units is not None:
        ctl.act_units = act_units
    put_setting(db, "control", ctl)
    db.commit()


async def test_act_mode_logs_each_settings_write(db):
    set_mode(db, "act")
    src = FakeEcobee()
    result = await controller.keep_settings(src, NOW)
    assert src.calls == 1 and result.mode == "act" and result.ok is False  # bed failed: retry later
    rows = actions(db, action="update_settings")
    assert [(r.unit_key, r.status) for r in rows] == [("up", "verified"), ("bed", "failed")]  # main was already off
    up, bed = rows
    assert up.actor == "controller" and up.mode == "act" and up.channel == "ecobee" and up.readback == OFF
    assert up.before == {"autoAway": True, "followMeComfort": False} and up.readback_ok is True
    assert bed.readback_ok is False and "did not read back" in bed.error
    assert set(result.ids) == {up.id, bed.id}


async def test_suggest_mode_only_suggests(db):
    set_mode(db, "suggest")
    put_snapshot(db, "main", NOW - timedelta(minutes=1), settings={"autoAway": True, "followMeComfort": False})
    put_snapshot(db, "up", NOW - timedelta(minutes=1), settings={"autoAway": "false", "followMeComfort": "true"})
    put_snapshot(db, "bed", NOW - timedelta(minutes=1), settings=OFF)
    db.commit()
    src = FakeEcobee()
    result = await controller.keep_settings(src, NOW)
    assert src.calls == 0 and result.ok and result.mode == "suggest"
    rows = actions(db, action="update_settings")
    assert [(r.unit_key, r.status, r.mode, r.channel) for r in rows] == [
        ("main", "suggested", "suggest", "none"), ("up", "suggested", "suggest", "none")]
    assert "Smart Away" in rows[0].reason and "Follow Me" in rows[1].reason


async def test_suggest_only_units_keep_act_mode_from_writing(db):
    set_mode(db, "act", act_units=["up"])  # ensure_settings writes every thermostat at once
    put_snapshot(db, "main", NOW - timedelta(minutes=1), settings={"autoAway": True})
    db.commit()
    src = FakeEcobee()
    await controller.keep_settings(src, NOW)
    assert src.calls == 0
    assert [(r.unit_key, r.status) for r in actions(db, action="update_settings")] == [("main", "suggested")]


async def test_off_mode_and_sources_without_the_method_do_nothing(db):
    set_mode(db, "off")
    src = FakeEcobee()
    assert (await controller.keep_settings(src, NOW)).ok and src.calls == 0
    set_mode(db, "act")
    assert (await controller.keep_settings(object(), NOW)).ids == []  # e.g. the simulator
    assert actions(db) == []


async def test_worker_checks_settings_at_start_then_once_a_day(db, monkeypatch):
    set_mode(db, "act")
    clock = {"now": datetime(2026, 10, 4, 15, 0, tzinfo=UTC)}
    monkeypatch.setattr(wmod, "utcnow", lambda: clock["now"])
    ok = [WriteResult(ok=True, channel="ecobee", readback=OFF, request={"unit_key": u, "settings": OFF, "noop": True})
          for u in ("main", "up", "bed")]
    inner = FakeEcobee(ok)
    w = wmod.Worker()
    w.source, w.source_kind = wmod.LockedSource(inner), "ecobee"  # type: ignore[assignment]
    await w.settings_step()
    assert inner.calls == 1  # at start
    await w.settings_step()
    assert inner.calls == 1  # not again the same day
    clock["now"] += timedelta(days=1)
    await w.settings_step()
    assert inner.calls == 2  # once a day
    set_mode(db, "suggest")
    await w.settings_step()
    set_mode(db, "act")
    await w.settings_step()
    assert inner.calls == 3  # back in act mode: checked again

    class Simulator:  # no ensure_settings: skipped silently
        kind = "simulator"

    w2 = wmod.Worker()
    w2.source, w2.source_kind = wmod.LockedSource(Simulator()), "simulator"  # type: ignore[arg-type, assignment]
    await w2.settings_step()
    assert w2._settings_done is None and actions(db, action="update_settings") == []


async def test_worker_retries_a_failed_settings_write_after_an_hour(db, monkeypatch):
    set_mode(db, "act")
    mono = {"t": 1000.0}
    monkeypatch.setattr(wmod.time, "monotonic", lambda: mono["t"])
    inner = FakeEcobee()  # bed fails
    w = wmod.Worker()
    w.source, w.source_kind = wmod.LockedSource(inner), "ecobee"  # type: ignore[assignment]
    await w.settings_step()
    mono["t"] += 600
    await w.settings_step()
    assert inner.calls == 1
    mono["t"] += wmod.SETTINGS_RETRY_S
    await w.settings_step()
    assert inner.calls == 2


@pytest.mark.parametrize("force", [False, True])
async def test_locked_source_passes_force_only_for_owner_resumes(force):
    seen: list[tuple] = []

    class New:
        kind = "ecobee"

        async def resume_program(self, unit_key: str, reason: str, force: bool = False) -> str:
            seen.append((unit_key, force))
            return "ok"

    class Old:  # an adapter without the parameter still works for the controller's call
        kind = "simulator"

        async def resume_program(self, unit_key: str, reason: str) -> str:
            seen.append((unit_key, "old"))
            return "ok"

    assert await wmod.LockedSource(New()).resume_program("up", "x", force=force) == "ok"
    assert seen == [("up", force)]
    if not force:
        assert await wmod.LockedSource(Old()).resume_program("bed", "x") == "ok"
        assert seen[-1] == ("bed", "old")

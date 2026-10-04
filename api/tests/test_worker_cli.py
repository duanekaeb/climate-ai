"""climate.cli: doctor, gen-key, set-password, backfill routing."""

from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import select

from climate import cli
from climate.store.app_settings import (
    AgentSettings,
    OwnerSettings,
    SourceSettings,
    beat,
    get_setting,
    put_setting,
)
from climate.store.orm import Job


def healthy(db) -> None:
    from climate.auth_service import set_owner_password

    beat(db, "worker", source="simulator", source_ok=True)
    beat(db, "agent", signed_in=True)
    db.commit()
    set_owner_password("correct horse battery staple")


def test_doctor_healthy(db, capsys):
    healthy(db)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "All checks passed." in out
    for name in ("database", "migrations", "secret key", "worker heartbeat", "agent heartbeat", "source",
                 "ANTHROPIC_API_KEY"):
        assert name in out
    assert "SKIP  homekit heartbeat" in out


def test_doctor_flags_api_key(db, capsys, monkeypatch):
    healthy(db)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-here")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  ANTHROPIC_API_KEY" in out and "sk-should-not-be-here" not in out


def test_doctor_flags_missing_heartbeats_and_source(db, capsys):
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True))
    put_setting(db, "agent", AgentSettings(enabled=False))
    db.commit()
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  worker heartbeat" in out
    assert "FAIL  homekit heartbeat" in out
    assert "SKIP  agent heartbeat" in out
    assert "not signed in" in out


def test_doctor_reports_worker_source_failure(db, capsys):
    beat(db, "worker", source="simulator", source_ok=False, source_error="RuntimeError: boom")
    beat(db, "agent")
    db.commit()
    assert cli.main(["doctor"]) == 1
    assert "RuntimeError: boom" in capsys.readouterr().out


def test_gen_key(capsys):
    assert cli.main(["gen-key"]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())
    Fernet(lines["CLIMATE_SECRET_KEY"].encode())
    assert len(lines["CLIMATE_JWT_SECRET"]) >= 32 and len(lines["CLIMATE_TOKEN_PEPPER"]) >= 32
    assert lines["CLIMATE_JWT_SECRET"] != lines["CLIMATE_TOKEN_PEPPER"]


def test_set_password(db, capsys):
    from climate.api.auth import verify_password

    answers = iter(["a much better password"] * 2)
    assert cli.cmd_set_password(None, prompt=lambda _: next(answers)) == 0
    db.expire_all()
    assert verify_password("a much better password", get_setting(db, "owner", OwnerSettings).password_hash)

    answers = iter(["first one!", "second one"])
    assert cli.cmd_set_password(None, prompt=lambda _: next(answers)) == 1
    assert cli.cmd_set_password(None, prompt=lambda _: "short") == 1


def test_backfill_queues_a_job_when_the_worker_owns_ecobee(db, capsys):
    put_setting(db, "source", SourceSettings(kind="ecobee"))
    beat(db, "worker", source="ecobee")
    db.commit()
    assert cli.main(["backfill", "--days", "120"]) == 0
    db.expire_all()
    job = db.execute(select(Job)).scalar_one()
    assert (job.kind, job.status, job.params) == ("backfill", "queued", {"days": 120})
    assert "queued backfill job" in capsys.readouterr().out
    assert cli.main(["backfill", "--days", "0"]) == 1


def test_migrate_is_idempotent(db, capsys):
    assert cli.main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_doctor_notes_a_managed_token_in_the_env(db, capsys, monkeypatch):
    from climate.config import get_settings

    healthy(db)
    monkeypatch.setattr(get_settings(), "agent_token", "cai_1_" + "x" * 43)
    monkeypatch.setattr(get_settings(), "mcp_token", "a-legacy-shared-token")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "env tokens" in ln)
    assert "CLIMATE_AGENT_TOKEN holds a managed API token (cai_)" in line and "revocable in the app" in line
    assert "CLIMATE_MCP_TOKEN is a legacy shared token" in line
    assert "x" * 43 not in out and "a-legacy-shared-token" not in out

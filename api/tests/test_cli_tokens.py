"""The auth commands of ``python -m climate.cli``: set-password (break-glass), create-token,
list-tokens, revoke-token, sign-out-everywhere. They call the same service functions as the
API with audit actor 'system', print a token exactly once, and never print a password or a
token hash. Spec: docs/specs/users-and-tokens.md (CLI)."""

from __future__ import annotations

import argparse
import io
import re

import pytest
from sqlalchemy import select, text

from climate import auth_service as svc
from climate import cli
from climate.api.security import AuthError, hash_token
from climate.store.db import session_scope
from climate.store.orm import ApiToken, AuthSession

PASSWORD = "correct horse battery"
HOME = "192.168.1.20"
TOKEN_ANYWHERE = re.compile(r"cai_[0-9a-f]+_[A-Za-z0-9_-]{43}")


def _audit(event_type: str):
    return [e for e in svc.list_audit(limit=500) if e.event_type == event_type]


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_create_token_prints_it_exactly_once_and_stores_only_a_hash(fresh_db, capsys):
    code, out, err = _run(capsys, "create-token", "--name", "Claude agent", "--role", "agent", "--expires-days", "90")
    assert code == 0 and err == ""
    found = TOKEN_ANYWHERE.findall(out)
    assert len(found) == 1
    raw = found[0]
    assert "home network only" in out and "expires" in out
    with session_scope() as s:
        row = s.execute(select(ApiToken)).scalar_one()
        dump = "\n".join(str(r._mapping) for r in s.execute(text("SELECT * FROM api_tokens")))
        dump += "\n".join(str(r._mapping) for r in s.execute(text("SELECT * FROM audit_events")))
    assert raw not in dump and row.token_hash == hash_token(raw) and row.token_hash not in out
    assert (row.name, row.role, row.local_only) == ("Claude agent", "agent", True)
    assert svc.check_api_token(raw, ip=HOME).role == "agent"
    created = _audit("token.create")
    assert len(created) == 1 and created[0].actor_type == "system" and created[0].target_id == str(row.id)


def test_create_token_print_only_and_allow_remote(fresh_db, capsys):
    code, out, _ = _run(capsys, "create-token", "--name", "shortcut", "--role", "control", "--allow-remote",
                        "--print-only-token")
    assert code == 0
    assert TOKEN_ANYWHERE.fullmatch(out.strip())
    assert svc.check_api_token(out.strip(), ip="203.0.113.9").role == "control"


def test_create_token_rejects_bad_input_without_printing_a_token(fresh_db, capsys):
    code, out, err = _run(capsys, "create-token", "--name", "x", "--expires-days", "0")
    assert code == 1 and "cai_" not in out and "expiry" in err
    with pytest.raises(SystemExit):
        cli.main(["create-token", "--name", "x", "--role", "owner"])
    capsys.readouterr()
    assert svc.list_api_tokens() == []


def test_list_tokens_shows_hints_never_secrets(fresh_db, capsys):
    code, out, _ = _run(capsys, "list-tokens")
    assert code == 0 and "No API tokens" in out
    row, raw = svc.create_api_token(svc.SYSTEM, name="viewer screen", role="viewer")
    code, out, _ = _run(capsys, "list-tokens")
    assert code == 0
    assert "viewer screen" in out and f"...{row.token_hint}" in out and "active" in out
    assert raw not in out and hash_token(raw) not in out and raw.split("_", 2)[2] not in out


def test_revoke_token(fresh_db, capsys):
    row, raw = svc.create_api_token(svc.SYSTEM, name="old", role="agent")
    code, out, _ = _run(capsys, "revoke-token", "--id", str(row.id))
    assert code == 0 and "Revoked" in out
    with pytest.raises(AuthError):
        svc.check_api_token(raw, ip=HOME)
    code, _, err = _run(capsys, "revoke-token", "--id", str(row.id))
    assert code == 1 and "No active API token" in err
    revoked = _audit("token.revoke")
    assert len(revoked) == 1 and revoked[0].actor_type == "system"
    _, out, _ = _run(capsys, "list-tokens")
    assert "revoked" in out


def test_set_password_prompts_twice_signs_everyone_out_and_audits(fresh_db, capsys):
    svc.setup(PASSWORD, ip=HOME)
    other = svc.login(PASSWORD, ip=HOME)
    answers = iter(["a new long password", "a new long password"])
    code = cli.cmd_set_password(argparse.Namespace(password_stdin=False), prompt=lambda _: next(answers))
    out = capsys.readouterr().out
    assert code == 0 and "Signed out 2 device(s)" in out and "a new long password" not in out
    with session_scope() as s:
        assert s.execute(select(AuthSession).where(AuthSession.revoked_at.is_(None))).first() is None
    with pytest.raises(AuthError):
        svc.check_access_token(other.access_token)
    svc.login("a new long password", ip=HOME)
    change = _audit("password.change")
    assert len(change) == 1 and change[0].actor_type == "system"
    assert change[0].payload == {"via": "cli", "sessions_revoked": 2}


def test_set_password_from_stdin(fresh_db, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped in password\n"))
    code, out, _ = _run(capsys, "set-password", "--password-stdin")
    assert code == 0 and "piped in password" not in out
    svc.login("piped in password", ip=HOME)


def test_set_password_refuses_mismatch_and_short_passwords(fresh_db, capsys):
    svc.setup(PASSWORD, ip=HOME)
    answers = iter(["one long password", "another long password"])
    assert cli.cmd_set_password(argparse.Namespace(password_stdin=False), prompt=lambda _: next(answers)) == 1
    answers = iter(["short", "short"])
    assert cli.cmd_set_password(argparse.Namespace(password_stdin=False), prompt=lambda _: next(answers)) == 1
    err = capsys.readouterr().err
    assert "do not match" in err and "at least 10" in err
    svc.login(PASSWORD, ip=HOME)  # unchanged
    assert _audit("password.change") == []


def test_sign_out_everywhere_keeps_api_tokens(fresh_db, capsys):
    svc.setup(PASSWORD, ip=HOME)
    svc.login(PASSWORD, ip=HOME)
    _row, raw = svc.create_api_token(svc.SYSTEM, name="agent", role="agent")
    code, out, _ = _run(capsys, "sign-out-everywhere")
    assert code == 0 and "Signed out 2 device(s)" in out
    assert svc.list_sessions() == []
    assert svc.check_api_token(raw, ip=HOME).role == "agent"
    done = _audit("auth.logout_all")
    assert len(done) == 1 and done[0].actor_type == "system" and done[0].payload == {"sessions_revoked": 2}


def test_gen_key_prints_the_auth_secrets(capsys):
    code, out, _ = _run(capsys, "gen-key")
    assert code == 0
    values = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert set(values) == {"CLIMATE_SECRET_KEY", "CLIMATE_JWT_SECRET", "CLIMATE_TOKEN_PEPPER"}
    assert len(values["CLIMATE_JWT_SECRET"]) >= 32 and len(values["CLIMATE_TOKEN_PEPPER"]) >= 32
    assert values["CLIMATE_JWT_SECRET"] != values["CLIMATE_TOKEN_PEPPER"]

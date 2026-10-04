"""The owner login service (climate.auth_service + climate.api.security), end to end on the
test database: passwords and tokens are never stored raw, refresh rotation and reuse
detection, sliding / absolute expiry, password change, brute-force guards, API tokens, the
scrypt -> Argon2id upgrade and WebSocket tickets. Spec: docs/specs/users-and-tokens.md."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import timedelta

import jwt
import pytest
from sqlalchemy import select, text

from climate import auth_service as svc
from climate.api.security import AuthError, hash_token, is_private_address
from climate.config import get_settings
from climate.store.db import session_scope
from climate.store.orm import Alert, ApiToken, AuditEvent, AuthSession
from climate.timeutil import utcnow
from tests.conftest import AGENT_TOKEN

PASSWORD = "correct horse battery"
HOME = "192.168.1.20"
TAILSCALE = "100.101.102.103"
INTERNET = "203.0.113.50"
TOKEN_RE = re.compile(r"^cai_[0-9a-f]+_[A-Za-z0-9_-]{43}$")


@pytest.fixture
def env(monkeypatch):
    """Set CLIMATE_* variables for one test; settings are re-read and restored afterwards."""

    def setenv(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(f"CLIMATE_{key.upper()}", value)
        get_settings.cache_clear()

    yield setenv
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def clock(monkeypatch):
    """Move the service's wall clock: ``clock(days=31)`` = that far from now."""
    base = utcnow()

    def move(**delta: float) -> None:
        monkeypatch.setattr(svc, "utcnow", lambda: base + timedelta(**delta))

    return move


def _raises(code: str, fn, *args, **kwargs) -> AuthError:
    with pytest.raises(AuthError) as info:
        fn(*args, **kwargs)
    assert info.value.code == code, (info.value.code, info.value.message)
    return info.value


def _setup() -> svc.Issued:
    return svc.setup(PASSWORD, ip=HOME, user_agent="pytest", device_name="first")


def _audit(event_type: str) -> list[AuditEvent]:
    return [e for e in svc.list_audit(limit=500) if e.event_type == event_type]


def _session(sid: int) -> AuthSession:
    with session_scope() as s:
        row = s.get(AuthSession, sid)
        assert row is not None
        return row


def _dump_everything() -> str:
    """Every stored auth artifact (settings, sessions, tokens, audit) as one string."""
    with session_scope() as s:
        parts = [json.dumps(r[0]) for r in s.execute(text("SELECT value FROM app_settings"))]
        for table in ("auth_sessions", "api_tokens", "audit_events"):
            parts += [json.dumps(dict(r._mapping), default=str) for r in s.execute(text(f"SELECT * FROM {table}"))]
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------
# storage: nothing raw
# ---------------------------------------------------------------------------------------


def test_password_refresh_and_api_tokens_are_never_stored_raw(fresh_db):
    issued = _setup()
    login = svc.login(PASSWORD, ip=HOME, device_name="phone")
    _row, raw_api = svc.create_api_token(svc.SYSTEM, name="agent", role="agent")
    stored = _dump_everything()
    for secret in (PASSWORD, issued.refresh_token, login.refresh_token, raw_api,
                   issued.refresh_token.split(".", 1)[1], raw_api.split("_", 2)[2]):
        assert secret not in stored
    with session_scope() as s:
        owner = s.execute(text("SELECT value->>'password_hash' FROM app_settings WHERE key = 'owner'")).scalar_one()
        session_row = s.get(AuthSession, issued.session_id)
        token_row = s.execute(select(ApiToken)).scalar_one()
    assert owner.startswith("$argon2id$")
    assert session_row.refresh_token_hash == hash_token(issued.refresh_token)
    assert token_row.token_hash == hash_token(raw_api)
    assert re.fullmatch(r"[0-9a-f]{64}", token_row.token_hash)


def test_access_token_carries_the_spec_claims(fresh_db):
    issued = _setup()
    claims = jwt.decode(issued.access_token, get_settings().jwt_secret, algorithms=["HS256"], audience="climate-ai")
    assert claims["sub"] == "owner" and claims["role"] == "owner" and claims["typ"] == "access"
    assert claims["sid"] == issued.session_id and claims["iss"] == "climate-ai" and claims["jti"]
    assert claims["exp"] - claims["iat"] == 15 * 60 == issued.expires_in
    principal = svc.check_access_token(issued.access_token)
    assert principal.role == "owner" and principal.session_id == issued.session_id
    assert principal.recently_authenticated  # the password was just typed
    forged = jwt.encode({**claims, "sid": issued.session_id}, "x" * 40, algorithm="HS256")
    _raises("NOT_AUTHENTICATED", svc.check_access_token, forged)
    expired = jwt.encode({**claims, "iat": claims["iat"] - 3600, "exp": claims["iat"] - 1800},
                         get_settings().jwt_secret, algorithm="HS256")
    _raises("TOKEN_EXPIRED", svc.check_access_token, expired)


# ---------------------------------------------------------------------------------------
# setup and the env password
# ---------------------------------------------------------------------------------------


def test_setup_only_once_only_from_home_and_needs_a_long_password(fresh_db, env):
    assert svc.setup_allowed(HOME) and svc.setup_allowed(TAILSCALE) and not svc.setup_allowed(INTERNET)
    _raises("SETUP_NOT_ALLOWED", svc.setup, PASSWORD, ip=INTERNET)
    _raises("WEAK_PASSWORD", svc.setup, "short", ip=HOME)
    assert not svc.password_set()
    issued = _setup()
    assert svc.password_set() and not svc.setup_allowed(HOME)
    _raises("ALREADY_SET", svc.setup, "another long password", ip=HOME)
    assert [e.target_id for e in _audit("auth.setup")] == [str(issued.session_id)]
    env(allow_remote_setup="true")
    assert not svc.setup_allowed(INTERNET)  # already set: nobody can claim it again


def test_remote_setup_when_allowed(fresh_db, env):
    env(allow_remote_setup="true")
    assert svc.setup_allowed(INTERNET)
    svc.setup(PASSWORD, ip=INTERNET)
    assert svc.password_set()


def test_env_password_is_imported_once_then_ignored(fresh_db, env):
    env(owner_password="from the environment")
    assert svc.password_set()
    with session_scope() as s:
        stored = s.execute(text("SELECT value->>'password_hash' FROM app_settings WHERE key = 'owner'")).scalar_one()
    assert stored.startswith("$argon2id$") and "from the environment" not in stored
    svc.login("from the environment", ip=HOME)
    env(owner_password="changed later")
    _raises("INVALID_CREDENTIALS", svc.login, "changed later", ip=HOME)
    svc.login("from the environment", ip=HOME)
    assert _audit("auth.setup")[0].actor_type == "system"


def test_auth_not_configured_without_secrets(fresh_db, env):
    _setup()
    env(jwt_secret="")
    assert "CLIMATE_JWT_SECRET" in (svc.auth_config_problem() or "")
    _raises("AUTH_NOT_CONFIGURED", svc.login, PASSWORD, ip=HOME)
    env(jwt_secret="j" * 40, token_pepper="too-short")
    assert "CLIMATE_TOKEN_PEPPER" in (svc.auth_config_problem() or "")
    _raises("AUTH_NOT_CONFIGURED", svc.check_api_token, "cai_1_abc", ip=HOME)
    _raises("AUTH_NOT_CONFIGURED", svc.refresh, "1.abc", ip=HOME)
    env(token_pepper="p" * 40)
    assert svc.auth_config_problem() is None
    assert svc.login(PASSWORD, ip=HOME).access_token
    assert svc.resolve_bearer(AGENT_TOKEN, ip=HOME).role == "agent"


# ---------------------------------------------------------------------------------------
# refresh rotation, reuse, expiry
# ---------------------------------------------------------------------------------------


def test_refresh_rotates_and_a_stale_refresh_token_revokes_the_family(fresh_db, monkeypatch):
    monkeypatch.setattr(svc, "REFRESH_GRACE", timedelta(0))  # the stale token arrives after the grace window
    first = _setup()
    second = svc.refresh(first.refresh_token, ip=HOME)
    assert second.session_id == first.session_id and second.refresh_token != first.refresh_token
    row = _session(first.session_id)
    assert row.rotation_counter == 1 and row.previous_token_hash == hash_token(first.refresh_token)
    svc.check_access_token(second.access_token)

    _raises("SESSION_REVOKED", svc.refresh, first.refresh_token, ip=INTERNET)  # the stale one again
    row = _session(first.session_id)
    assert row.revoked_at is not None and row.revoked_reason == "reuse_detected"
    _raises("SESSION_REVOKED", svc.refresh, second.refresh_token, ip=HOME)  # the thief's copy dies too
    _raises("SESSION_REVOKED", svc.check_access_token, second.access_token)
    reuse = _audit("auth.refresh_reuse")
    assert len(reuse) == 1 and reuse[0].ip == INTERNET and reuse[0].target_id == str(first.session_id)


def test_a_wrong_refresh_secret_does_not_end_the_session(fresh_db):
    issued = _setup()
    sid_hex = issued.refresh_token.split(".", 1)[0]
    well_formed_but_wrong = f"{sid_hex}." + "A" * 43
    for bad in (None, "", "garbage", f"{sid_hex}.not-the-secret", well_formed_but_wrong, "zz.secret", f"{sid_hex}."):
        _raises("NOT_AUTHENTICATED", svc.refresh, bad, ip=INTERNET)
    assert _session(issued.session_id).revoked_at is None
    svc.refresh(issued.refresh_token, ip=HOME)


def test_sliding_expiry(fresh_db, clock):
    issued = _setup()
    clock(days=29)
    again = svc.refresh(issued.refresh_token, ip=HOME)  # slides 30 more days
    assert again.refresh_max_age == 30 * 86400
    clock(days=58)
    again = svc.refresh(again.refresh_token, ip=HOME)
    clock(days=58 + 31)  # idle for longer than 30 days
    _raises("SESSION_REVOKED", svc.refresh, again.refresh_token, ip=HOME)
    assert svc.list_sessions() == []


def test_absolute_expiry_caps_the_sliding_window(fresh_db, clock):
    issued = _setup()
    token = issued.refresh_token
    for day in (25, 50, 75):
        clock(days=day)
        result = svc.refresh(token, ip=HOME)
        token = result.refresh_token
    assert result.refresh_max_age == 15 * 86400  # 90 days from sign-in, not 75 + 30
    clock(days=89)
    token = svc.refresh(token, ip=HOME).refresh_token
    clock(days=90, seconds=1)
    _raises("SESSION_REVOKED", svc.refresh, token, ip=HOME)


# ---------------------------------------------------------------------------------------
# devices, reauth, password change
# ---------------------------------------------------------------------------------------


def test_change_password_signs_out_every_other_device(fresh_db):
    here = _setup()
    other = svc.login(PASSWORD, ip=TAILSCALE, device_name="laptop")
    me = svc.check_access_token(here.access_token)
    _raises("WEAK_PASSWORD", svc.change_password, me, PASSWORD, "short", ip=HOME)
    _raises("INVALID_CREDENTIALS", svc.change_password, me, "not it", "a brand new password", ip=HOME)
    svc.change_password(me, PASSWORD, "a brand new password", ip=HOME)
    _raises("SESSION_REVOKED", svc.check_access_token, other.access_token)
    _raises("SESSION_REVOKED", svc.refresh, other.refresh_token, ip=TAILSCALE)
    assert _session(other.session_id).revoked_reason == "password_change"
    assert svc.check_access_token(here.access_token).session_id == here.session_id  # this device stays
    _raises("INVALID_CREDENTIALS", svc.login, PASSWORD, ip=HOME)
    svc.login("a brand new password", ip=HOME)
    change = _audit("password.change")
    assert len(change) == 1 and change[0].payload == {"sessions_revoked": 1}
    assert "a brand new password" not in _dump_everything()


def test_reauth_stamps_the_device_and_wrong_password_keeps_it_signed_in(fresh_db, clock):
    issued = _setup()
    clock(minutes=11)
    me = svc.check_access_token(issued.access_token)
    assert not me.recently_authenticated
    _raises("INVALID_CREDENTIALS", svc.reauth, me, "nope", ip=HOME)
    fresh = svc.reauth(me, PASSWORD, ip=HOME)
    assert fresh.recently_authenticated
    assert len(_audit("auth.reauth")) == 1
    token = svc.issue_ws_ticket(fresh)[0]
    assert svc.redeem_ws_ticket(token) is not None


def test_sessions_list_revoke_logout_and_logout_all(fresh_db):
    a = _setup()
    b = svc.login(PASSWORD, ip=HOME, user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)")
    c = svc.login(PASSWORD, ip=HOME, device_name="kitchen\x00 tablet")
    names = {s.id: s.device_name for s in svc.list_sessions()}
    assert names == {a.session_id: "first", b.session_id: "iPhone", c.session_id: "kitchen tablet"}
    me = svc.check_access_token(a.access_token)
    assert svc.revoke_session(me, b.session_id, ip=HOME) is True
    assert svc.revoke_session(me, b.session_id, ip=HOME) is False
    _raises("SESSION_REVOKED", svc.check_access_token, b.access_token)

    svc.logout(c.refresh_token.split(".")[0] + ".wrong", ip=HOME)  # a guess signs nothing out
    assert _session(c.session_id).revoked_at is None
    svc.logout(c.refresh_token, ip=HOME)
    assert _session(c.session_id).revoked_reason == "logout"

    d = svc.login(PASSWORD, ip=HOME)
    assert svc.logout_all(me, ip=HOME) == 2  # a and d
    assert svc.list_sessions() == []
    _raises("SESSION_REVOKED", svc.check_access_token, d.access_token)
    assert [e.payload for e in _audit("auth.logout_all")] == [{"sessions_revoked": 2}]
    assert len(_audit("session.revoke")) == 1 and len(_audit("auth.logout")) == 1


# ---------------------------------------------------------------------------------------
# brute force
# ---------------------------------------------------------------------------------------


def test_per_address_limiter_counts_failures_only(fresh_db, monkeypatch):
    _setup()
    for _ in range(9):
        _raises("INVALID_CREDENTIALS", svc.login, "wrong", ip="10.0.0.5")
    svc.login(PASSWORD, ip="10.0.0.5")  # a success gives its reservation back
    _raises("INVALID_CREDENTIALS", svc.login, "wrong", ip="10.0.0.5")  # the 10th failure
    _raises("TOO_MANY_ATTEMPTS", svc.login, PASSWORD, ip="10.0.0.5")  # even the right password waits
    svc.login(PASSWORD, ip="10.0.0.6")  # another address is unaffected
    now = svc._monotonic()
    monkeypatch.setattr(svc, "_monotonic", lambda: now + 301)
    svc.login(PASSWORD, ip="10.0.0.5")
    assert len(_audit("auth.login_failed")) == 10
    assert not _audit("auth.login_paused")  # home addresses never count toward the pause


def test_public_failures_pause_internet_sign_in_but_home_always_works(fresh_db, env):
    env(public_login_max_failures="5", login_pause_minutes="15")
    _setup()
    for i in range(5):  # a distributed guesser: one try per address
        _raises("INVALID_CREDENTIALS", svc.login, "guess", ip=f"203.0.113.{i + 1}")
    _raises("LOGIN_PAUSED", svc.login, PASSWORD, ip="198.51.100.9")  # the owner, from the internet
    assert svc.login(PASSWORD, ip=HOME).access_token  # at home it always works
    assert svc.login(PASSWORD, ip=TAILSCALE).access_token  # and over Tailscale
    paused = _audit("auth.login_paused")
    assert len(paused) == 1 and paused[0].payload["failures"] == 5
    with session_scope() as s:
        alert = s.execute(select(Alert).where(Alert.kind == "auth:login_paused")).scalar_one()
    assert alert.level == "warn"
    assert all(e.payload["public"] is True for e in _audit("auth.login_failed"))
    with session_scope() as s:  # the pause ends after login_pause_minutes
        s.execute(text("UPDATE audit_events SET ts = ts - interval '16 minutes' WHERE event_type = 'auth.login_paused'"))
    assert svc.login(PASSWORD, ip="198.51.100.9").access_token


def test_scrypt_hash_is_upgraded_to_argon2id_on_sign_in(fresh_db):
    salt = os.urandom(16)
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**14, r=8, p=1, maxmem=2**26, dklen=32)
    legacy = f"scrypt${2**14}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    with session_scope() as s:
        s.execute(text("UPDATE app_settings SET value = jsonb_set(value, '{password_hash}', to_jsonb(CAST(:h AS text)))"
                       " WHERE key = 'owner'"), {"h": legacy})
        if s.execute(text("SELECT 1 FROM app_settings WHERE key = 'owner'")).first() is None:
            s.execute(text("INSERT INTO app_settings (key, value, updated_by) VALUES ('owner', "
                           "jsonb_build_object('password_hash', CAST(:h AS text)), 'test')"), {"h": legacy})
    assert svc.password_set()
    _raises("INVALID_CREDENTIALS", svc.login, "wrong password", ip=HOME)
    svc.login(PASSWORD, ip=HOME)
    with session_scope() as s:
        stored = s.execute(text("SELECT value->>'password_hash' FROM app_settings WHERE key = 'owner'")).scalar_one()
    assert stored.startswith("$argon2id$")
    svc.login(PASSWORD, ip=HOME)


def test_login_before_setup_costs_a_check_and_says_so(fresh_db):
    _raises("PASSWORD_NOT_SET", svc.login, "anything at all", ip=HOME)


# ---------------------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------------------


def test_api_token_shown_once_hashed_and_checked_live(fresh_db, clock):
    row, raw = svc.create_api_token(svc.SYSTEM, name="  Claude   agent ", role="agent", expires_in_days=30)
    assert TOKEN_RE.match(raw) and raw.startswith(f"cai_{row.id:x}_")
    assert row.token_hint == raw[-4:] and row.name == "Claude agent" and row.local_only
    listed = svc.list_api_tokens()
    assert [t.id for t in listed] == [row.id] and not hasattr(listed[0], "token")
    who = svc.resolve_bearer(raw, ip=HOME)
    assert (who.role, who.kind, who.token_id, who.label) == ("agent", "api_token", row.id, "Claude agent")
    _raises("FORBIDDEN", svc.check_api_token, raw, ip=INTERNET)  # home network only
    _raises("NOT_AUTHENTICATED", svc.check_api_token, raw[:-1] + ("A" if raw[-1] != "A" else "B"), ip=HOME)
    _raises("NOT_AUTHENTICATED", svc.check_api_token, f"cai_{row.id + 1:x}_" + raw.split("_", 2)[2], ip=HOME)
    created = _audit("token.create")
    assert len(created) == 1 and created[0].actor_type == "system" and raw not in json.dumps(created[0].payload)
    clock(days=31)
    _raises("NOT_AUTHENTICATED", svc.check_api_token, raw, ip=HOME)  # expired


def test_api_token_last_used_is_written_at_most_once_a_minute(fresh_db, clock):
    _row, raw = svc.create_api_token(svc.SYSTEM, name="viewer", role="viewer")
    clock(seconds=0)
    svc.check_api_token(raw, ip="10.0.0.7")
    first = svc.list_api_tokens()[0]
    assert first.last_used_at is not None and first.last_used_ip == "10.0.0.7"
    clock(seconds=30)
    svc.check_api_token(raw, ip="10.0.0.8")
    assert svc.list_api_tokens()[0].last_used_at == first.last_used_at
    assert svc.list_api_tokens()[0].last_used_ip == "10.0.0.7"
    clock(seconds=61)
    svc.check_api_token(raw, ip="10.0.0.8")
    assert svc.list_api_tokens()[0].last_used_ip == "10.0.0.8"


def test_revoked_token_is_refused_and_remote_tokens_work_from_the_internet(fresh_db):
    issued = _setup()
    me = svc.check_access_token(issued.access_token)
    row, raw = svc.create_api_token(me, name="phone shortcut", role="control", local_only=False)
    assert svc.check_api_token(raw, ip=INTERNET).role == "control"
    assert svc.revoke_api_token(me, row.id, ip=HOME) is True
    assert svc.revoke_api_token(me, row.id, ip=HOME) is False
    _raises("NOT_AUTHENTICATED", svc.check_api_token, raw, ip=HOME)
    revoked = _audit("token.revoke")
    assert len(revoked) == 1 and revoked[0].actor_type == "owner" and revoked[0].actor_id == issued.session_id


def test_creating_a_token_needs_a_recent_password_and_a_valid_role(fresh_db, clock):
    issued = _setup()
    me = svc.check_access_token(issued.access_token)
    _raises("INVALID_REQUEST", svc.create_api_token, me, name="x", role="owner")
    _raises("INVALID_REQUEST", svc.create_api_token, me, name="   ", role="agent")
    _raises("INVALID_REQUEST", svc.create_api_token, me, name="x", role="agent", expires_in_days=0)
    clock(minutes=11)
    _raises("REAUTHENTICATION_REQUIRED", svc.create_api_token, me, name="late", role="agent")
    agent = svc.resolve_bearer(AGENT_TOKEN, ip=HOME)
    _raises("FORBIDDEN", svc.create_api_token, agent, name="escalate", role="control")
    _row, raw = svc.create_api_token(svc.SYSTEM, name="t", role="viewer")
    _raises("FORBIDDEN", svc.create_api_token, svc.check_api_token(raw, ip=HOME), name="escalate", role="control")


def test_legacy_env_token_is_agent_and_home_only(fresh_db):
    who = svc.resolve_bearer(AGENT_TOKEN, ip="172.18.0.4")  # the agent container on the Docker network
    assert (who.role, who.kind) == ("agent", "env_token")
    _raises("FORBIDDEN", svc.resolve_bearer, AGENT_TOKEN, ip=INTERNET)
    _raises("NOT_AUTHENTICATED", svc.resolve_bearer, "", ip=HOME)
    _raises("NOT_AUTHENTICATED", svc.resolve_bearer, "not-a-token", ip=HOME)


# ---------------------------------------------------------------------------------------
# WebSocket tickets
# ---------------------------------------------------------------------------------------


def test_ws_ticket_is_single_use_short_lived_and_dies_with_its_session(fresh_db, monkeypatch):
    issued = _setup()
    me = svc.check_access_token(issued.access_token)
    ticket, ttl = svc.issue_ws_ticket(me)
    assert ttl == 30
    got = svc.redeem_ws_ticket(ticket)
    assert got is not None and got.role == "owner" and got.session_id == issued.session_id
    assert svc.redeem_ws_ticket(ticket) is None  # single use

    late, _ = svc.issue_ws_ticket(me)
    now = svc._monotonic()
    monkeypatch.setattr(svc, "_monotonic", lambda: now + 31)
    assert svc.redeem_ws_ticket(late) is None  # expired
    monkeypatch.setattr(svc, "_monotonic", lambda: now)

    revoked, _ = svc.issue_ws_ticket(me)
    svc.logout(principal=me)
    assert svc.redeem_ws_ticket(revoked) is None  # the device signed out since
    _row, raw = svc.create_api_token(svc.SYSTEM, name="viewer", role="viewer")
    tok_ticket, _ = svc.issue_ws_ticket(svc.check_api_token(raw, ip=HOME))
    assert svc.redeem_ws_ticket(tok_ticket).role == "viewer"
    assert svc.redeem_ws_ticket("") is None and svc.redeem_ws_ticket("unknown") is None


# ---------------------------------------------------------------------------------------
# addresses
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("ip,private", [
    ("10.1.2.3", True), ("172.16.0.1", True), ("172.31.255.255", True), ("172.32.0.1", False),
    ("192.168.0.10", True), ("127.0.0.1", True), ("100.64.0.1", True), ("100.127.255.254", True),
    ("100.128.0.1", False), ("::1", True), ("fd7a:115c:a1e0::1", True), ("fe80::1%eth0", True),
    ("::ffff:192.168.1.5", True), ("::ffff:8.8.8.8", False), ("8.8.8.8", False), ("2606:4700::1111", False),
    ("203.0.113.5", False), ("testclient", False), ("", False), (None, False),
])
def test_private_address_ranges(ip, private):
    assert is_private_address(ip) is private

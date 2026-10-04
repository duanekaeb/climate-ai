"""The owner login over HTTP: first-run setup, sign-in, the refresh cookie and its rotation,
sign-out, signed-in devices, change password, re-authentication, brute-force limits and the
bearer resolver's error codes. Spec: docs/specs/users-and-tokens.md. (The service layer's own
tests live in test_auth_service.py; the full role matrix in test_route_guard.py.)"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest

from climate import auth_service
from climate.api.security import encode_access_token
from climate.config import get_settings
from climate.timeutil import utcnow
from tests.conftest import AGENT_TOKEN, OWNER_PASSWORD, PUBLIC_ADDR, make_client, sign_in

COOKIE = auth_service.REFRESH_COOKIE
NEW_PASSWORD = "a brand new password"


def code(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def set_cookie_header(r) -> SimpleCookie:
    jar = SimpleCookie()
    for raw in r.headers.get_list("set-cookie"):
        jar.load(raw)
    return jar


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def second_device(password: str = OWNER_PASSWORD, name: str = "phone") -> Iterator:
    """Another signed-in device: a fresh TestClient that logged in."""
    with make_client() as c:
        sign_in(c, password, device_name=name)
        yield c


# --- first run ------------------------------------------------------------------------------


def test_state_before_any_password(client):
    r = client.get("/api/auth/state")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False, "role": None, "password_set": False, "setup_allowed": True}


def test_setup_issues_a_bearer_and_a_scoped_refresh_cookie(client):
    r = client.post("/api/auth/setup", json={"password": OWNER_PASSWORD, "device_name": "Kitchen iPad"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == get_settings().access_ttl_minutes * 60
    assert body["access_token"].count(".") == 2  # a JWT
    morsel = set_cookie_header(r)[COOKIE]
    assert morsel["path"] == "/api/auth" and morsel["httponly"] and morsel["samesite"].lower() == "lax"
    assert not morsel["secure"]  # CLIMATE_COOKIE_SECURE is off in tests
    assert body["access_token"] not in r.headers.get("set-cookie", "")

    me = client.get("/api/auth/me", headers=bearer(body["access_token"])).json()
    assert me == {"role": "owner", "session_id": body["session_id"], "token_id": None, "token_name": None,
                  "recently_authenticated": True}
    state = client.get("/api/auth/state", headers=bearer(body["access_token"])).json()
    assert state == {"authenticated": True, "role": "owner", "password_set": True, "setup_allowed": False}


def test_setup_only_works_once(client):
    assert client.post("/api/auth/setup", json={"password": OWNER_PASSWORD}).status_code == 200
    again = client.post("/api/auth/setup", json={"password": "someone else entirely"})
    assert again.status_code == 409 and code(again) == "ALREADY_SET"
    assert client.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 200
    assert code(client.post("/api/auth/login", json={"password": "someone else entirely"})) == "INVALID_CREDENTIALS"


def test_setup_enforces_the_minimum_length(client):
    assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
    r = client.post("/api/auth/setup", json={"password": "nine char"})  # 8-9: the server's own rule
    assert r.status_code == 422 and code(r) == "WEAK_PASSWORD"
    assert client.get("/api/auth/state").json()["password_set"] is False


def test_setup_from_the_internet_is_refused_unless_allowed(fresh_db, monkeypatch):
    with make_client(addr=PUBLIC_ADDR) as c:
        assert c.get("/api/auth/state").json()["setup_allowed"] is False
        r = c.post("/api/auth/setup", json={"password": OWNER_PASSWORD})
        assert r.status_code == 403 and code(r) == "SETUP_NOT_ALLOWED"
        assert c.get("/api/auth/state").json()["password_set"] is False
        monkeypatch.setattr(get_settings(), "allow_remote_setup", True)
        assert c.get("/api/auth/state").json()["setup_allowed"] is True
        assert c.post("/api/auth/setup", json={"password": OWNER_PASSWORD}).status_code == 200


def test_login_before_setup_is_conflict(client):
    r = client.post("/api/auth/login", json={"password": OWNER_PASSWORD})
    assert r.status_code == 409 and code(r) == "PASSWORD_NOT_SET"


def test_auth_not_configured_is_503(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "jwt_secret", "")
    r = client.post("/api/auth/setup", json={"password": OWNER_PASSWORD})
    assert r.status_code == 503 and code(r) == "AUTH_NOT_CONFIGURED"


# --- the bearer -----------------------------------------------------------------------------


def test_bearer_errors_have_codes(owner, client):
    assert code(client.get("/api/status")) == "NOT_AUTHENTICATED"
    r = client.get("/api/status", headers=bearer("not-a-token"))
    assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"
    assert r.headers["www-authenticate"] == "Bearer"
    r = client.get("/api/status", headers={"Authorization": "Basic b3duZXI6cGFzcw=="})
    assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"
    forged = owner.headers["Authorization"][:-4] + "AAAA"
    assert code(client.get("/api/status", headers={"Authorization": forged})) == "NOT_AUTHENTICATED"
    sid = owner.get("/api/auth/me").json()["session_id"]
    expired, _ = encode_access_token(sid, now=utcnow() - timedelta(hours=1))
    r = client.get("/api/status", headers=bearer(expired))
    assert r.status_code == 401 and code(r) == "TOKEN_EXPIRED"
    # a public route treats a bad bearer as anonymous instead of failing
    assert client.get("/api/auth/state", headers=bearer(expired)).json()["authenticated"] is False


def test_legacy_env_token_is_the_agent(agent):
    me = agent.get("/api/auth/me").json()
    assert me["role"] == "agent" and me["token_name"] == "CLIMATE_AGENT_TOKEN" and me["session_id"] is None
    assert agent.get("/api/auth/state").json()["role"] == "agent"
    assert agent.post("/api/auth/logout").status_code == 204  # nothing to sign out
    assert agent.get("/api/status").status_code == 200
    with make_client(addr=PUBLIC_ADDR, token=AGENT_TOKEN) as remote:
        r = remote.get("/api/status")
        assert r.status_code == 403 and code(r) == "FORBIDDEN"


# --- refresh --------------------------------------------------------------------------------


def test_refresh_rotates_the_cookie_and_mints_a_new_access_token(owner):
    old_cookie = owner.cookies.get(COOKIE)
    r = owner.post("/api/auth/refresh")
    assert r.status_code == 200, r.text
    new_cookie = owner.cookies.get(COOKIE)
    assert new_cookie and new_cookie != old_cookie
    assert set_cookie_header(r)[COOKIE]["path"] == "/api/auth"
    assert owner.get("/api/status", headers=bearer(r.json()["access_token"])).status_code == 200
    assert r.json()["session_id"] == owner.get("/api/auth/me").json()["session_id"]


def test_reusing_an_old_refresh_token_signs_the_device_out(owner, monkeypatch):
    from datetime import timedelta

    from climate import auth_service

    monkeypatch.setattr(auth_service, "REFRESH_GRACE", timedelta(0))  # well after the rotation
    stolen = owner.cookies.get(COOKIE)
    assert owner.post("/api/auth/refresh").status_code == 200
    owner.cookies.clear()
    owner.cookies.set(COOKIE, stolen, path="/api/auth")
    r = owner.post("/api/auth/refresh")
    assert r.status_code == 401 and code(r) == "SESSION_REVOKED"
    cleared = set_cookie_header(r)[COOKIE]
    assert cleared["max-age"] == "0" and cleared["path"] == "/api/auth"
    assert code(owner.get("/api/status")) == "SESSION_REVOKED"  # the access token died with it
    events = owner_audit_types()
    assert "auth.refresh_reuse" in events


def test_refresh_without_a_cookie_or_cross_origin(owner, client):
    r = client.post("/api/auth/refresh")
    assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"
    evil = owner.post("/api/auth/refresh", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and code(evil) == "FORBIDDEN"
    assert owner.post("/api/auth/refresh", headers={"Origin": "http://testserver"}).status_code == 200


# --- sign out -------------------------------------------------------------------------------


def test_logout_revokes_this_device_and_clears_the_cookie(owner):
    assert owner.get("/api/status").status_code == 200
    r = owner.post("/api/auth/logout")
    assert r.status_code == 204
    assert COOKIE not in owner.cookies
    r = owner.get("/api/status")
    assert r.status_code == 401 and code(r) == "SESSION_REVOKED"
    owner.headers.pop("Authorization")
    assert code(owner.post("/api/auth/refresh")) == "NOT_AUTHENTICATED"
    sign_in(owner)
    assert owner.get("/api/setup").status_code == 200


def test_logout_with_only_the_cookie(owner):
    access = owner.headers.pop("Authorization")
    assert owner.post("/api/auth/logout").status_code == 204
    assert code(owner.get("/api/status", headers={"Authorization": access})) == "SESSION_REVOKED"


def test_cross_origin_logout_is_refused(owner):
    r = owner.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert owner.get("/api/status").status_code == 200


def test_bearer_writes_need_no_origin_check(owner):
    # CSRF needs an ambient credential; a bearer header is not one, so Origin does not matter.
    assert owner.post("/api/control/mode", json={"mode": "off"}, headers={"Origin": "https://elsewhere.example"}).status_code == 200


def test_logout_all_ends_every_device(owner):
    with second_device() as phone:
        assert phone.get("/api/status").status_code == 200
        r = owner.post("/api/auth/logout-all")
        assert r.status_code == 204
        assert COOKIE not in owner.cookies
        assert code(owner.get("/api/status")) == "SESSION_REVOKED"
        assert code(phone.get("/api/status")) == "SESSION_REVOKED"
        assert code(phone.post("/api/auth/refresh")) == "SESSION_REVOKED"


def test_old_logout_everywhere_route_is_gone(owner):
    assert owner.post("/api/auth/logout-everywhere").status_code in (404, 405)


# --- devices --------------------------------------------------------------------------------


def test_sessions_list_and_revoke_another_device(owner):
    with second_device(name="Olive's iPad") as tablet:
        tablet_id = tablet.get("/api/auth/me").json()["session_id"]
        rows = owner.get("/api/auth/sessions").json()
        assert {r["device_name"] for r in rows} == {"pytest", "Olive's iPad"}
        mine = owner.get("/api/auth/me").json()["session_id"]
        assert [r["id"] for r in rows if r["current"]] == [mine]
        assert all(r["ip"] == "127.0.0.1" for r in rows)
        assert "refresh_token_hash" not in rows[0]

        assert owner.delete(f"/api/auth/sessions/{tablet_id}").status_code == 204
        assert code(tablet.get("/api/status")) == "SESSION_REVOKED"
        assert [r["id"] for r in owner.get("/api/auth/sessions").json()] == [mine]
        r = owner.delete(f"/api/auth/sessions/{tablet_id}")
        assert r.status_code == 404 and code(r) == "NOT_FOUND"


# --- password -------------------------------------------------------------------------------


def test_change_password_signs_out_the_other_devices(owner):
    with second_device() as phone:
        r = owner.post("/api/auth/change-password", json={"current_password": "wrong one!", "new_password": NEW_PASSWORD})
        assert r.status_code == 403 and code(r) == "INVALID_CREDENTIALS"
        r = owner.post("/api/auth/change-password", json={"current_password": OWNER_PASSWORD, "new_password": "too short"})
        assert r.status_code == 422 and code(r) == "WEAK_PASSWORD"
        r = owner.post("/api/auth/change-password", json={"current_password": OWNER_PASSWORD, "new_password": NEW_PASSWORD})
        assert r.status_code == 204
        assert owner.get("/api/setup").status_code == 200  # this device stays signed in
        assert code(phone.get("/api/status")) == "SESSION_REVOKED"
    with make_client() as c:
        assert code(c.post("/api/auth/login", json={"password": OWNER_PASSWORD})) == "INVALID_CREDENTIALS"
        assert c.post("/api/auth/login", json={"password": NEW_PASSWORD}).status_code == 200
    assert "password.change" in owner_audit_types()


def test_break_glass_password_reset_signs_everyone_out(owner):
    auth_service.set_owner_password(NEW_PASSWORD)
    assert code(owner.get("/api/status")) == "SESSION_REVOKED"
    owner.headers.pop("Authorization")
    sign_in(owner, NEW_PASSWORD)
    assert owner.get("/api/setup").status_code == 200


def test_reauth_opens_the_recent_window(owner, monkeypatch):
    assert owner.get("/api/auth/me").json()["recently_authenticated"] is True  # signing in counts
    later = utcnow() + timedelta(minutes=get_settings().reauth_window_minutes + 1)
    monkeypatch.setattr("climate.auth_service.utcnow", lambda: later)
    assert owner.get("/api/auth/me").json()["recently_authenticated"] is False
    r = owner.post("/api/auth/reauth", json={"password": "not it at all"})
    assert r.status_code == 403 and code(r) == "INVALID_CREDENTIALS"
    r = owner.post("/api/auth/reauth", json={"password": OWNER_PASSWORD})
    assert r.status_code == 200 and r.json()["recently_authenticated"] is True


# --- brute force ----------------------------------------------------------------------------


def test_login_is_limited_per_address(owner):
    for _ in range(10):
        r = owner.post("/api/auth/login", json={"password": "not the password"})
        assert r.status_code == 401 and code(r) == "INVALID_CREDENTIALS"
    r = owner.post("/api/auth/login", json={"password": OWNER_PASSWORD})  # even the right one
    assert r.status_code == 429 and code(r) == "TOO_MANY_ATTEMPTS"
    assert owner.get("/api/status").status_code == 200  # signed-in devices are unaffected
    with make_client(addr=("192.168.1.20", 50000)) as other:  # another address is not
        assert other.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 200


def test_successful_logins_do_not_count(owner):
    for _ in range(12):
        assert owner.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 200


def test_internet_sign_in_pauses_but_home_still_works(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_login_max_failures", 5)
    with make_client(addr=PUBLIC_ADDR) as attacker:
        for _ in range(5):
            assert code(attacker.post("/api/auth/login", json={"password": "guess guess"})) == "INVALID_CREDENTIALS"
    with make_client(addr=("198.51.100.9", 50000)) as owner_abroad:
        r = owner_abroad.post("/api/auth/login", json={"password": OWNER_PASSWORD})
        assert r.status_code == 429 and code(r) == "LOGIN_PAUSED"
    assert owner.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 200  # from home
    assert owner.get("/api/status").status_code == 200
    types = owner_audit_types()
    assert "auth.login_paused" in types and types.count("auth.login_failed") == 5


# --- audit ----------------------------------------------------------------------------------


def owner_audit_types() -> list[str]:
    return [e.event_type for e in auth_service.list_audit(limit=500)]


def test_sign_in_actions_are_audited_without_secrets(owner):
    owner.post("/api/auth/login", json={"password": "wrong password!"})
    owner.post("/api/auth/refresh")
    owner.post("/api/auth/logout")
    rows = auth_service.list_audit(limit=50)
    types = [r.event_type for r in rows]
    for expected in ("auth.setup", "auth.login_failed", "auth.logout"):
        assert expected in types
    blob = repr([(r.payload, r.target_id, r.actor_label) for r in rows])
    assert OWNER_PASSWORD not in blob and "wrong password!" not in blob
    assert all(r.ip == "127.0.0.1" for r in rows if r.event_type != "auth.login_paused")


@pytest.mark.parametrize("path", ["/api/auth/sessions", "/api/audit", "/api/tokens"])
def test_owner_surfaces_are_not_for_tokens(agent, path):
    r = agent.get(path)
    assert r.status_code == 403 and code(r) == "FORBIDDEN"


def test_the_pre_upgrade_cookie_grants_nothing_and_is_expired(owner, client):
    client.cookies.set("climate_session", "an-old-signed-cookie")
    r = client.get("/api/status")
    assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"
    r = client.post("/api/auth/login", json={"password": OWNER_PASSWORD})
    assert r.status_code == 200
    old = set_cookie_header(r)["climate_session"]
    assert old["max-age"] == "0" and old["path"] == "/"


def test_a_lost_refresh_reply_is_retried_within_the_grace_window(owner):
    """A refresh the browser never saw (reload mid-refresh, dropped reply) is retried with the
    token it replaced: within the grace window that rotates again instead of signing out, and a
    thief who held the newer token is still caught when they use it afterwards."""
    from datetime import timedelta

    from climate import auth_service

    first = owner.cookies.get(COOKIE)
    assert owner.post("/api/auth/refresh").status_code == 200
    unseen = owner.cookies.get(COOKIE)
    owner.cookies.clear()
    owner.cookies.set(COOKIE, first, path="/api/auth")
    r = owner.post("/api/auth/refresh")
    assert r.status_code == 200, r.text
    assert owner.get("/api/status", headers=bearer(r.json()["access_token"])).status_code == 200
    # the reply nobody saw is now the previous token: presenting it later is theft
    auth_service.REFRESH_GRACE, saved = timedelta(0), auth_service.REFRESH_GRACE
    try:
        owner.cookies.clear()
        owner.cookies.set(COOKIE, unseen, path="/api/auth")
        assert code(owner.post("/api/auth/refresh")) == "SESSION_REVOKED"
    finally:
        auth_service.REFRESH_GRACE = saved


def test_requests_through_cloudflare_never_count_as_home(owner, client):
    """Every request through the Cloudflare tunnel carries CF-Connecting-IP: it is treated as
    internet even when the proxy address looks private (a FORWARDED_ALLOW_IPS mistake)."""
    from climate.api.auth import client_ip
    from climate.api.security import is_private_address
    from starlette.requests import Request

    def req(cf: str) -> Request:
        return Request({"type": "http", "headers": [(b"cf-connecting-ip", cf.encode())], "client": ("172.20.0.5", 1)})

    assert client_ip(req("203.0.113.9")) == "203.0.113.9"
    assert not is_private_address(client_ip(req("192.168.1.20")) or "")  # spoofed private value
    r = client.get("/api/auth/state", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.json()["setup_allowed"] is False


def test_first_run_setup_refuses_a_rebinding_host_name(fresh_db):
    """Before a password exists, setup needs a home-network host name: a page on another domain
    that rebinds to the server's LAN address must not be able to choose the password."""
    with make_client() as c:
        r = c.post("/api/auth/setup", json={"password": OWNER_PASSWORD}, headers={"Host": "evil.example.com"})
        assert r.status_code == 403 and code(r) == "SETUP_NOT_ALLOWED"
        assert c.get("/api/auth/state", headers={"Host": "evil.example.com"}).json()["setup_allowed"] is False
        for host in ("192.168.1.20:8470", "localhost:8470", "climate.local", "[::1]:8470"):
            assert c.get("/api/auth/state", headers={"Host": host}).json()["setup_allowed"] is True, host
        assert c.post("/api/auth/setup", json={"password": OWNER_PASSWORD},
                      headers={"Host": "192.168.1.20:8470"}).status_code == 200


def test_setup_host_reads_only_the_host_header_and_accepts_single_label_names(fresh_db):
    """X-Forwarded-Host is a header the rebinding page's own script can set: only Host counts.
    A single-label name (``nas``, ``climate``) is a home-network name nobody can register."""
    with make_client() as c:
        spoofed = {"Host": "evil.example.com", "X-Forwarded-Host": "localhost"}
        r = c.post("/api/auth/setup", json={"password": OWNER_PASSWORD}, headers=spoofed)
        assert r.status_code == 403 and code(r) == "SETUP_NOT_ALLOWED"
        assert c.get("/api/auth/state", headers=spoofed).json()["setup_allowed"] is False
        for host in ("nas:8470", "climate", "nas."):
            assert c.get("/api/auth/state", headers={"Host": host}).json()["setup_allowed"] is True, host
        assert c.post("/api/auth/setup", json={"password": OWNER_PASSWORD},
                      headers={"Host": "nas:8470"}).status_code == 200


def test_reauth_from_the_internet_honours_the_pause(fresh_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_login_max_failures", 5)
    with make_client() as home:
        sign_in(home)
    with make_client(addr=PUBLIC_ADDR) as abroad:
        sign_in(abroad, device_name="laptop abroad")
        with make_client(addr=("198.51.100.30", 50000)) as attacker:
            for _ in range(5):
                assert code(attacker.post("/api/auth/login", json={"password": "guess guess"})) == "INVALID_CREDENTIALS"
        r = abroad.post("/api/auth/reauth", json={"password": OWNER_PASSWORD})
        assert r.status_code == 429 and code(r) == "LOGIN_PAUSED"
        r = abroad.post("/api/auth/change-password",
                        json={"current_password": OWNER_PASSWORD, "new_password": NEW_PASSWORD})
        assert r.status_code == 429 and code(r) == "LOGIN_PAUSED"
        assert abroad.get("/api/status").status_code == 200  # still signed in

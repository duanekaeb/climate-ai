"""Owner sign-in: first-run setup, login throttling, logout, cookie and bearer roles."""

from __future__ import annotations

from collections import defaultdict

import pytest

from climate.api import auth as auth_mod
from tests.conftest import AGENT_TOKEN, OWNER_PASSWORD


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    # The throttle is per-process state keyed by client IP ("testclient"); isolate it.
    monkeypatch.setattr(auth_mod, "_FAILS", defaultdict(list))


def test_state_before_any_password(client):
    r = client.get("/api/auth/state")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False, "role": None, "password_set": False}


def test_setup_sets_cookie_and_only_works_once(client):
    r = client.post("/api/auth/setup", json={"password": OWNER_PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"authenticated": True, "role": "owner", "password_set": True}
    assert auth_mod.COOKIE in client.cookies
    assert client.get("/api/auth/state").json()["role"] == "owner"

    again = client.post("/api/auth/setup", json={"password": "someone else entirely"})
    assert again.status_code == 409
    # the original password still works
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 200
    assert client.post("/api/auth/login", json={"password": "someone else entirely"}).status_code == 401


def test_setup_rejects_short_password(client):
    assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
    assert client.get("/api/auth/state").json()["password_set"] is False


def test_login_before_setup_is_conflict(client):
    assert client.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 409


def test_logout_then_login(owner):
    assert owner.get("/api/status").status_code == 200
    r = owner.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False, "role": None, "password_set": True}
    assert owner.get("/api/status").status_code == 401

    assert owner.post("/api/auth/login", json={"password": "wrong password!"}).status_code == 401
    r = owner.post("/api/auth/login", json={"password": OWNER_PASSWORD})
    assert r.status_code == 200
    assert r.json()["role"] == "owner"
    assert owner.get("/api/status").status_code == 200
    assert owner.get("/api/setup").status_code == 200  # cookie = owner role


def test_login_is_throttled_per_ip(owner):
    owner.post("/api/auth/logout")
    for _ in range(5):
        assert owner.post("/api/auth/login", json={"password": "not the password"}).status_code == 401
    # sixth attempt is refused even with the right password
    assert owner.post("/api/auth/login", json={"password": OWNER_PASSWORD}).status_code == 429


def test_bearer_token_is_agent_role(agent):
    r = agent.get("/api/auth/state")
    assert r.json()["authenticated"] is True
    assert r.json()["role"] == "agent"
    # logging out does not end a bearer caller's access
    out = agent.post("/api/auth/logout").json()
    assert out["role"] == "agent"
    assert agent.get("/api/status").status_code == 200


def test_wrong_bearer_and_forged_cookie_are_anonymous(client):
    bad = client.get("/api/status", headers={"Authorization": "Bearer not-" + AGENT_TOKEN})
    assert bad.status_code == 401
    client.cookies.set(auth_mod.COOKIE, "forged.cookie.value")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/auth/state").json()["authenticated"] is False


def test_cross_origin_owner_write_is_refused(owner):
    r = owner.post("/api/control/mode", json={"mode": "off"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = owner.post("/api/control/mode", json={"mode": "off"}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200

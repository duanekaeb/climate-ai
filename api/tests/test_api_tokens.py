"""API tokens and the audit log over HTTP: create (password re-entered recently, shown once,
stored only as an HMAC), list, use, last-used tracking, expiry, revoke, home-network-only, and
``GET /api/audit``. Spec: docs/specs/users-and-tokens.md."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from climate.config import get_settings
from climate.store.db import session_scope
from climate.store.orm import ApiToken
from climate.timeutil import utcnow
from tests.conftest import PUBLIC_ADDR, make_client


def code(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create(owner, **body) -> dict:
    r = owner.post("/api/tokens", json={"name": "Wall panel", "role": "control", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_shows_the_token_once_and_stores_only_a_hash(owner):
    made = create(owner, expires_in_days=30)
    raw = made["token"]
    assert raw.startswith("cai_") and made["token_hint"] == raw[-4:]
    assert made["role"] == "control" and made["local_only"] is True and made["expires_at"] is not None
    listed = owner.get("/api/tokens").json()
    assert [t["id"] for t in listed] == [made["id"]]
    assert "token" not in listed[0] and listed[0]["token_hint"] == raw[-4:]
    with session_scope() as s:
        row = s.get(ApiToken, made["id"])
        stored = [str(v) for v in (row.token_hash, row.token_hint, row.name, row.revoked_reason)]
    assert row.token_hash != raw and len(row.token_hash) == 64
    assert not any(raw in v for v in stored)
    secret = raw.split("_", 2)[2]
    assert all(secret not in str(e["payload"]) for e in owner.get("/api/audit").json())


def test_a_token_works_for_its_role_only(owner):
    raw = create(owner, role="viewer")["token"]
    with make_client(token=raw) as viewer:
        assert viewer.get("/api/status").status_code == 200
        me = viewer.get("/api/auth/me").json()
        assert me["role"] == "viewer" and me["token_name"] == "Wall panel" and me["session_id"] is None
        r = viewer.post("/api/control/hold", json={"unit_key": "main", "heat_f": 68, "cool_f": 76})
        assert r.status_code == 403 and code(r) == "FORBIDDEN"
        assert code(viewer.post("/api/tokens", json={"name": "escalate", "role": "control"})) == "FORBIDDEN"
        assert code(viewer.get("/api/tokens")) == "FORBIDDEN"


def test_creating_needs_the_password_re_entered_recently(owner, monkeypatch):
    later = utcnow() + timedelta(minutes=get_settings().reauth_window_minutes + 1)
    monkeypatch.setattr("climate.auth_service.utcnow", lambda: later)
    r = owner.post("/api/tokens", json={"name": "Late", "role": "agent"})
    assert r.status_code == 403 and code(r) == "REAUTHENTICATION_REQUIRED"
    assert owner.post("/api/auth/reauth", json={"password": "correct horse battery"}).status_code == 200
    assert owner.post("/api/tokens", json={"name": "Late", "role": "agent"}).status_code == 201


def test_bodies_are_validated(owner):
    assert owner.post("/api/tokens", json={"name": "x", "role": "owner"}).status_code == 422
    assert owner.post("/api/tokens", json={"name": "", "role": "agent"}).status_code == 422
    assert owner.post("/api/tokens", json={"name": "x", "expires_in_days": 0}).status_code == 422
    assert owner.get("/api/tokens").json() == []


def test_last_use_is_recorded(owner):
    made = create(owner)
    with make_client(token=made["token"]) as panel:
        assert panel.get("/api/status").status_code == 200
    row = owner.get("/api/tokens").json()[0]
    assert row["last_used_at"] is not None and row["last_used_ip"] == "127.0.0.1"


def test_revoke_stops_the_token_at_once(owner):
    made = create(owner)
    with make_client(token=made["token"]) as panel:
        assert panel.get("/api/status").status_code == 200
        assert owner.delete(f"/api/tokens/{made['id']}").status_code == 204
        r = panel.get("/api/status")
        assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"
    again = owner.delete(f"/api/tokens/{made['id']}")
    assert again.status_code == 404 and code(again) == "NOT_FOUND"
    assert owner.get("/api/tokens").json()[0]["revoked_at"] is not None
    types = [e["event_type"] for e in owner.get("/api/audit").json()]
    assert {"token.create", "token.revoke"} <= set(types)


def test_expired_and_forged_tokens_are_refused(owner):
    made = create(owner)
    with session_scope() as s:
        s.execute(update(ApiToken).where(ApiToken.id == made["id"]).values(expires_at=utcnow() - timedelta(seconds=1)))
    with make_client() as c:
        assert code(c.get("/api/status", headers=bearer(made["token"]))) == "NOT_AUTHENTICATED"
        forged = made["token"][:-6] + "AAAAAA"
        assert code(c.get("/api/status", headers=bearer(forged))) == "NOT_AUTHENTICATED"
        assert code(c.get("/api/status", headers=bearer("cai_zz_nothing"))) == "NOT_AUTHENTICATED"


def test_home_network_only_unless_allowed(owner):
    home_only = create(owner, name="Home only")["token"]
    anywhere = create(owner, name="Anywhere", local_only=False)["token"]
    with make_client(addr=PUBLIC_ADDR) as remote:
        r = remote.get("/api/status", headers=bearer(home_only))
        assert r.status_code == 403 and code(r) == "FORBIDDEN"
        assert remote.get("/api/status", headers=bearer(anywhere)).status_code == 200
    with session_scope() as s:
        flags = dict(s.execute(select(ApiToken.name, ApiToken.local_only)).all())
    assert flags == {"Home only": True, "Anywhere": False}


def test_audit_log_filters_and_limits(owner):
    for i in range(3):
        create(owner, name=f"t{i}")
    rows = owner.get("/api/audit").json()
    assert [r["event_type"] for r in rows][:3] == ["token.create"] * 3  # newest first
    assert rows[-1]["event_type"] == "auth.setup"
    assert rows[0]["actor_type"] == "owner" and rows[0]["actor_label"] == "pytest"
    only = owner.get("/api/audit", params={"event_type": "auth.setup"}).json()
    assert [r["event_type"] for r in only] == ["auth.setup"]
    assert len(owner.get("/api/audit", params={"limit": 2}).json()) == 2
    assert owner.get("/api/audit", params={"limit": 0}).status_code == 422


def test_a_revoked_cai_token_pasted_into_the_env_is_refused(owner, monkeypatch):
    """A managed token set as CLIMATE_AGENT_TOKEN / CLIMATE_MCP_TOKEN is checked live, not
    matched as the legacy env token, so revoking it in the app stops it at once."""
    made = create(owner, name="agent via .env", role="agent")
    monkeypatch.setattr(get_settings(), "agent_token", made["token"])
    monkeypatch.setattr(get_settings(), "mcp_token", made["token"])
    with make_client(token=made["token"]) as agent:
        me = agent.get("/api/auth/me").json()
        assert me["role"] == "agent" and me["token_id"] == made["id"]
        assert owner.delete(f"/api/tokens/{made['id']}").status_code == 204
        r = agent.get("/api/status")
        assert r.status_code == 401 and code(r) == "NOT_AUTHENTICATED"

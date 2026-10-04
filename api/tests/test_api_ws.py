"""/api/ws: authentication (single-use tickets for browsers, bearer headers for tokens) and close
codes, the hello, pings, liveness re-checks, real LISTEN/NOTIFY fan-out, and a hub that
tolerates the database being down."""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from climate import events
from climate.api.routers import ws as ws_mod
from climate.store.db import session_scope


def test_unauthenticated_socket_is_closed_4401(client):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/ws") as sock:
        sock.receive_json()
    assert exc.value.code == ws_mod.CLOSE_UNAUTHORIZED


def _ticket(c) -> str:
    r = c.post("/api/auth/ws-ticket")
    assert r.status_code == 200, r.text
    assert r.json()["expires_in"] == 30
    return r.json()["ticket"]


def _closed_with(c, url: str, **kw) -> int:
    with pytest.raises(WebSocketDisconnect) as exc, c.websocket_connect(url, **kw) as sock:
        sock.receive_json()
    return exc.value.code


def test_bearer_callers_get_the_hello(owner, agent, viewer, control):
    for c in (owner, agent, viewer, control):
        with c.websocket_connect("/api/ws") as sock:
            assert sock.receive_json() == {"type": "status", "id": None}


def test_a_ticket_opens_one_socket(owner, client):
    ticket = _ticket(owner)
    with client.websocket_connect(f"/api/ws?ticket={ticket}") as sock:  # no bearer, like a browser
        assert sock.receive_json() == {"type": "status", "id": None}
    assert _closed_with(client, f"/api/ws?ticket={ticket}") == ws_mod.CLOSE_UNAUTHORIZED  # single use
    assert _closed_with(client, "/api/ws?ticket=made-up") == ws_mod.CLOSE_UNAUTHORIZED
    assert client.post("/api/auth/ws-ticket").status_code == 401


def test_cross_origin_ticket_socket_is_refused(owner, client):
    ticket = _ticket(owner)
    evil = {"Origin": "https://evil.example"}
    assert _closed_with(client, f"/api/ws?ticket={ticket}", headers=evil) == ws_mod.CLOSE_FORBIDDEN
    same = {"Origin": "http://testserver"}
    with client.websocket_connect(f"/api/ws?ticket={ticket}", headers=same) as sock:
        assert sock.receive_json()["type"] == "status"


def test_ticket_of_a_signed_out_device_is_refused(owner, client):
    ticket = _ticket(owner)
    assert owner.post("/api/auth/logout").status_code == 204
    assert _closed_with(client, f"/api/ws?ticket={ticket}") == ws_mod.CLOSE_UNAUTHORIZED


def test_bad_bearer_socket_is_closed_4401(client):
    headers = {"Authorization": "Bearer nope"}
    assert _closed_with(client, "/api/ws", headers=headers) == ws_mod.CLOSE_UNAUTHORIZED


def test_signing_out_drops_an_open_socket(owner, client, monkeypatch):
    monkeypatch.setattr(ws_mod, "PING_S", 0.05)
    with client.websocket_connect(f"/api/ws?ticket={_ticket(owner)}") as sock:
        assert sock.receive_json()["type"] == "status"
        assert sock.receive_json()["type"] == "ping"
        assert owner.post("/api/auth/logout-all").status_code == 204
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(100):
                assert sock.receive_json()["type"] == "ping"
    assert exc.value.code == ws_mod.CLOSE_UNAUTHORIZED


def test_ping_keeps_the_socket_alive(owner, monkeypatch):
    monkeypatch.setattr(ws_mod, "PING_S", 0.05)
    with owner.websocket_connect("/api/ws") as sock:
        assert sock.receive_json()["type"] == "status"
        assert sock.receive_json() == {"type": "ping", "id": None}


def test_notify_reaches_the_browser(owner, monkeypatch):
    monkeypatch.setattr(ws_mod, "PING_S", 0.2)  # bounds every receive below
    deadline = time.monotonic() + 15
    while not ws_mod.hub.listening and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ws_mod.hub.listening, "hub never LISTENed on the test database"
    with owner.websocket_connect("/api/ws") as sock:
        assert sock.receive_json()["type"] == "status"
        with session_scope() as s:
            events.publish(s, "alert", 42)
        got = None
        for _ in range(50):
            msg = sock.receive_json()
            if msg["type"] != "ping":
                got = msg
                break
        assert got == {"type": "alert", "id": 42}


def test_libpq_url():
    assert ws_mod.libpq_url("postgresql+psycopg://climate:p%40ss@db:5432/climate") == "postgresql://climate:p%40ss@db:5432/climate"


def test_hub_tolerates_database_down(monkeypatch):
    class Down:
        database_url = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"

    monkeypatch.setattr(ws_mod, "get_settings", lambda: Down())

    async def main() -> None:
        hub = ws_mod.Hub()
        started = time.monotonic()
        await hub.start()
        assert time.monotonic() - started < 0.5  # never blocks startup
        await asyncio.sleep(0.3)
        assert hub.listening is False
        assert await hub.broadcast('{"type": "status", "id": null}') == 0
        await asyncio.wait_for(hub.stop(), 5)

    asyncio.run(main())


def test_home_only_token_from_the_internet_is_closed_4403(fresh_db):
    from tests.conftest import PUBLIC_ADDR, make_client, mint_token

    with make_client(addr=PUBLIC_ADDR, token=mint_token("viewer")) as remote:
        assert _closed_with(remote, "/api/ws") == ws_mod.CLOSE_FORBIDDEN

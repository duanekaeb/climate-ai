"""/api/ws: authentication close codes, the hello, pings, real LISTEN/NOTIFY fan-out, and a hub
that tolerates the database being down."""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from climate import events
from climate.api.routers import ws as ws_mod
from climate.store.db import session_scope


def test_unauthenticated_socket_is_closed_4401(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/ws") as sock:
            sock.receive_json()
    assert exc.value.code == ws_mod.CLOSE_UNAUTHORIZED


def test_owner_and_agent_get_the_hello(owner, agent):
    for c in (owner, agent):
        with c.websocket_connect("/api/ws") as sock:
            assert sock.receive_json() == {"type": "status", "id": None}


def test_cross_origin_cookie_socket_is_refused(owner):
    with pytest.raises(WebSocketDisconnect) as exc:
        with owner.websocket_connect("/api/ws", headers={"Origin": "https://evil.example"}) as sock:
            sock.receive_json()
    assert exc.value.code == ws_mod.CLOSE_FORBIDDEN


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

"""Websocket /api/ws: LISTEN climate_events (Postgres) and fan out to signed-in browsers.

Writers ``climate.events.publish`` a tiny ``{"type", "id"}`` NOTIFY inside their transaction;
the hub holds one autocommit psycopg ``AsyncConnection`` that LISTENs on
``climate.events.CHANNEL`` and forwards each event to every connected websocket, and the
clients refetch what changed. The listener reconnects with exponential backoff and tolerates
the database being down at startup (``start`` never blocks on it).

State is kept per event loop. Production runs one loop; tests may run several apps at once
(each TestClient has its own loop), and a websocket can only be written from its own loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import psycopg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from starlette.concurrency import run_in_threadpool

from climate import auth_service
from climate.api import auth
from climate.api.auth import Principal
from climate.api.schemas import WsEvent
from climate.api.security import AuthError
from climate.config import get_settings
from climate.events import CHANNEL

log = logging.getLogger(__name__)
router = APIRouter(tags=["ws"])

PING_S = 30.0  # keep-alive message interval (also detects dead sockets)
SEND_TIMEOUT_S = 5.0  # a client slower than this is dropped
CONNECT_TIMEOUT_S = 10
BACKOFF_MAX_S = 60.0
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
PING_MESSAGE = '{"type":"ping","id":null}'


def libpq_url(sqlalchemy_url: str) -> str:
    """'postgresql+psycopg://u:p@h:5432/db' -> 'postgresql://u:p@h:5432/db' for psycopg."""
    return make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)


class _Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.lock = asyncio.Lock()  # the hub and the ping loop both send

    async def send_text(self, text: str) -> bool:
        try:
            async with self.lock:
                await asyncio.wait_for(self.ws.send_text(text), SEND_TIMEOUT_S)
            return True
        except Exception:  # noqa: BLE001 - any failure means the socket is gone
            return False


@dataclass
class _LoopState:
    task: asyncio.Task[None] | None = None
    clients: set[_Client] = field(default_factory=set)
    listening: bool = False


class Hub:
    def __init__(self) -> None:
        self._loops: dict[asyncio.AbstractEventLoop, _LoopState] = {}

    def _state(self, create: bool = True) -> _LoopState | None:
        loop = asyncio.get_running_loop()
        st = self._loops.get(loop)
        if st is None and create:
            st = self._loops[loop] = _LoopState()
        return st

    @property
    def listening(self) -> bool:
        """True once a listener holds a LISTEN on the database (any loop)."""
        return any(st.listening for st in list(self._loops.values()))

    async def start(self) -> None:
        """Open one async psycopg connection, LISTEN climate_events, broadcast each payload."""
        st = self._state()
        assert st is not None
        if st.task is None or st.task.done():
            st.task = asyncio.create_task(self._listen(st), name="climate-ws-hub")

    async def stop(self) -> None:
        st = self._loops.pop(asyncio.get_running_loop(), None)
        if st is None:
            return
        if st.task is not None:
            st.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await st.task
        for client in list(st.clients):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.ws.close(code=1001), 2)
        st.clients.clear()

    def register(self, ws: WebSocket) -> _Client:
        st = self._state()
        assert st is not None
        client = _Client(ws)
        st.clients.add(client)
        return client

    def unregister(self, client: _Client) -> None:
        st = self._state(create=False)
        if st is not None:
            st.clients.discard(client)

    async def broadcast(self, payload: str) -> int:
        """Send one event payload to every client on this loop; returns how many got it."""
        st = self._state(create=False)
        return 0 if st is None else await self._fanout(st, payload)

    async def _fanout(self, st: _LoopState, payload: str) -> int:
        if not st.clients:
            return 0
        try:
            text = WsEvent.model_validate_json(payload).model_dump_json()
        except ValidationError:
            log.debug("ignoring a malformed %s payload", CHANNEL)
            return 0
        clients = list(st.clients)
        results = await asyncio.gather(*(c.send_text(text) for c in clients))
        for client, ok in zip(clients, results, strict=True):
            if not ok:
                st.clients.discard(client)
        return sum(results)

    async def _listen(self, st: _LoopState) -> None:
        backoff, failures = 1.0, 0
        while True:
            try:
                url = libpq_url(get_settings().database_url)
                conn = await psycopg.AsyncConnection.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S)
                async with conn:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(CHANNEL)))
                    st.listening = True
                    if failures:
                        log.info("websocket hub: listening again")
                    backoff, failures = 1.0, 0
                    async for note in conn.notifies():
                        await self._fanout(st, note.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep retrying; the app must keep serving
                failures += 1
                (log.warning if failures == 1 else log.debug)(
                    "websocket hub: database unavailable (%s); retrying in %.0f s", type(exc).__name__, backoff
                )
            finally:
                st.listening = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


hub = Hub()


def _origin_ok(websocket: WebSocket) -> bool:
    """Ticket sockets come from the browser, so they must come from our own origin
    (cross-site WebSocket hijacking guard). No Origin header (native clients) passes."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


async def _authenticate(websocket: WebSocket) -> tuple[Principal | None, int, str]:
    """Who is connecting: ``?ticket=`` (browsers; single use, same origin only) or an
    ``Authorization: Bearer`` header (API tokens, the agent). Returns (principal, close code,
    reason); the principal is None when the socket must be closed with that code."""
    ticket = websocket.query_params.get("ticket")
    if ticket:
        if not _origin_ok(websocket):
            return None, CLOSE_FORBIDDEN, "Cross-origin websocket refused."
        principal = await run_in_threadpool(auth_service.redeem_ws_ticket, ticket)
        if principal is None:
            return None, CLOSE_UNAUTHORIZED, "Ticket unknown, used or expired."
        return principal, 0, ""
    try:
        principal = await run_in_threadpool(auth.resolve_caller, websocket)
    except AuthError as err:  # 403: a token refused here (home network only); else not signed in
        return None, CLOSE_FORBIDDEN if err.status == 403 else CLOSE_UNAUTHORIZED, err.message[:120]
    if principal is None:
        return None, CLOSE_UNAUTHORIZED, "Sign in required."
    return principal, 0, ""


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Any signed-in caller (owner or any API token role) may listen; events carry only a
    type and an id, and clients refetch through the role-checked REST routes. Liveness is
    re-checked on every keep-alive ping, so a revoked device or token is dropped within
    ``PING_S``."""
    principal, code, reason = await _authenticate(websocket)
    # Accept first so the browser sees the close code (a refused handshake is just 1006).
    await websocket.accept()
    if principal is None:
        await websocket.close(code=code, reason=reason)
        return
    client = hub.register(websocket)
    try:
        if not await client.send_text(WsEvent(type="status").model_dump_json()):
            return
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=PING_S)
            except TimeoutError:
                if not await run_in_threadpool(auth_service.principal_is_live, principal):
                    with contextlib.suppress(Exception):
                        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Signed out.")
                    return
                if not await client.send_text(PING_MESSAGE):
                    return
                continue
            if message["type"] == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return
    finally:
        hub.unregister(client)

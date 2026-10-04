"""Websocket /api/ws: LISTEN climate_events (Postgres) and fan out to signed-in browsers."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["ws"])


class Hub:
    async def start(self) -> None:
        """Open one async psycopg connection, LISTEN climate_events, broadcast each payload."""

    async def stop(self) -> None:
        pass


hub = Hub()

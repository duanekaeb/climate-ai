"""Engine and session factory (SQLAlchemy 2.0, sync, psycopg 3).

Everything uses the sync ``Session``. FastAPI endpoints are plain ``def`` (threadpool);
async loops wrap DB work in ``asyncio.to_thread`` when it could block for long.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from climate.config import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(
        url or get_settings().database_url,
        pool_pre_ping=True,
        # app + worker + homekit share one Postgres; keep the total well under max_connections
        # (the TimescaleDB image tunes it down to 25 on 2 GB hosts).
        pool_size=3,
        max_overflow=4,
        future=True,
    )


@lru_cache
def _factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transaction scope: commit on success, roll back on error."""
    session = _factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def notify(session: Session, channel: str, payload: str) -> None:
    """pg_notify inside the caller's transaction (delivered on commit). Payload < 8000 bytes."""
    session.execute(text("SELECT pg_notify(:c, :p)"), {"c": channel, "p": payload[:7900]})

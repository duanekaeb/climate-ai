"""Shared test fixtures.

Every pytest session creates its own throwaway database (so several test runs can share one
Postgres), migrates and seeds it, and drops it at the end. Set CLIMATE_TEST_ADMIN_URL to a
role that may CREATE DATABASE (default: the dev role on 127.0.0.1).

Fixtures:
- ``db``        a Session on a clean database (tables truncated + reseeded per test)
- ``client``    FastAPI TestClient, not signed in
- ``owner``     TestClient signed in as the owner (password 'correct horse battery')
- ``agent``     TestClient with the agent bearer token
"""

from __future__ import annotations

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

ADMIN_URL = os.environ.get("CLIMATE_TEST_ADMIN_URL", "postgresql+psycopg://climate:climate@127.0.0.1:5432/postgres")
OWNER_PASSWORD = "correct horse battery"
AGENT_TOKEN = "test-agent-token"
KEEP = {"schema_migrations", "units", "rooms", "sensors"}

os.environ.setdefault("CLIMATE_SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("CLIMATE_SESSION_SECRET", "test-session-secret")
os.environ.setdefault("CLIMATE_AGENT_TOKEN", AGENT_TOKEN)
os.environ.setdefault("CLIMATE_WEB_DIR", "/nonexistent")
os.environ.setdefault("CLIMATE_MIGRATE_ON_START", "0")
os.environ.pop("CLIMATE_OWNER_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)


def _reset_caches() -> None:
    from climate.config import get_settings
    from climate.store import db as dbmod

    get_settings.cache_clear()
    if dbmod.get_engine.cache_info().currsize:
        dbmod.get_engine().dispose()
    dbmod.get_engine.cache_clear()
    dbmod._factory.cache_clear()


@pytest.fixture(scope="session")
def database_url():
    name = f"climate_t_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    os.environ["CLIMATE_DATABASE_URL"] = url
    _reset_caches()
    from climate.house import seed
    from climate.store.db import session_scope
    from climate.store.migrate import migrate

    migrate()
    with session_scope() as s:
        seed(s)
    yield url
    _reset_caches()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def reset_db() -> None:
    from climate.house import seed
    from climate.store.db import get_engine, session_scope

    with get_engine().begin() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))]
        wipe = [t for t in tables if t not in KEEP]
        if wipe:
            conn.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in wipe) + " RESTART IDENTITY CASCADE"))
        conn.execute(text("UPDATE units SET ecobee_identifier = NULL, homekit_device_id = NULL, thermostat_model = NULL, equipment = '{}'::jsonb"))
        conn.execute(text("UPDATE sensors SET ecobee_sensor_id = NULL, homekit_aid = NULL"))
    with session_scope() as s:
        seed(s)


@pytest.fixture
def db(database_url):
    from climate.store.db import _factory

    reset_db()
    session = _factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(database_url):
    from fastapi.testclient import TestClient

    from climate.api.app import create_app

    reset_db()
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def owner(client):
    r = client.post("/api/auth/setup", json={"password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["authenticated"] is True
    return client


@pytest.fixture
def agent(database_url):
    from fastapi.testclient import TestClient

    from climate.api.app import create_app

    reset_db()
    with TestClient(create_app(), headers={"Authorization": f"Bearer {AGENT_TOKEN}"}) as c:
        yield c

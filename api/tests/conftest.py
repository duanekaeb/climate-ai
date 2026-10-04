"""Shared test fixtures.

Every pytest session creates its own throwaway database (so several test runs can share one
Postgres), migrates and seeds it, and drops it at the end. Set CLIMATE_TEST_ADMIN_URL to a
role that may CREATE DATABASE (default: the dev role on 127.0.0.1).

Fixtures (all share one database reset per test, so asking for several never wipes another's
sign-in):
- ``fresh_db``      truncate + reseed the database and clear the in-memory auth state
- ``db``            a Session on that clean database
- ``client``        FastAPI TestClient, not signed in, calling from 127.0.0.1 (a private address)
- ``owner``         a separate TestClient that sends the owner's bearer access token (password
                    ``OWNER_PASSWORD`` set through ``/api/auth/setup``; the refresh cookie is in
                    its cookie jar)
- ``agent``         TestClient with the legacy ``CLIMATE_AGENT_TOKEN`` bearer (role agent)
- ``agent_token`` / ``viewer_token`` / ``control_token``  raw ``cai_`` API tokens of each role
- ``viewer`` / ``control``  TestClients sending those tokens
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
LOCAL_ADDR = ("127.0.0.1", 50000)  # TestClient's peer address: a private (home network) caller
PUBLIC_ADDR = ("203.0.113.7", 50000)  # TEST-NET-3: stands in for a caller on the internet
KEEP = {"schema_migrations", "units", "rooms", "sensors"}

os.environ.setdefault("CLIMATE_SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("CLIMATE_SESSION_SECRET", "test-session-secret")
# Test-only signing key and pepper (at least 32 characters, as the service requires).
os.environ.setdefault("CLIMATE_JWT_SECRET", "test-jwt-secret-0123456789abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("CLIMATE_TOKEN_PEPPER", "test-token-pepper-0123456789abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("CLIMATE_SETUP_HOSTS", "testserver")  # the TestClient's Host header
os.environ.setdefault("CLIMATE_AGENT_TOKEN", AGENT_TOKEN)
os.environ.setdefault("CLIMATE_WEB_DIR", "/nonexistent")
os.environ.setdefault("CLIMATE_MIGRATE_ON_START", "0")
os.environ.pop("CLIMATE_OWNER_PASSWORD", None)
os.environ.pop("CLIMATE_ALLOW_REMOTE_SETUP", None)
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


def reset_auth_memory() -> None:
    """Per-process sign-in state (per-address limiter, internet pause, WebSocket tickets)."""
    from climate import auth_service

    auth_service.reset_memory_state()


@pytest.fixture
def fresh_db(database_url):
    """One reset per test, shared by every fixture below (function-scoped fixtures are cached
    per test, so ``owner`` + ``agent`` + ``db`` see the same database and the same sign-in)."""
    reset_db()
    reset_auth_memory()
    yield
    reset_auth_memory()


def make_client(*, addr: tuple[str, int] = LOCAL_ADDR, token: str | None = None):
    """A TestClient for a fresh app, calling from ``addr``, optionally with a bearer token.
    Use it as a context manager (it runs the app's lifespan)."""
    from fastapi.testclient import TestClient

    from climate.api.app import create_app

    headers = {"Authorization": f"Bearer {token}"} if token else None
    return TestClient(create_app(), client=addr, headers=headers)


def sign_in(c, password: str = OWNER_PASSWORD, device_name: str = "pytest") -> dict:
    """Sign ``c`` in (first-run setup when no password is set yet, else login) and make it send
    the new access token. Returns the AccessTokenOut JSON."""
    state = c.get("/api/auth/state").json()
    route = "/api/auth/login" if state["password_set"] else "/api/auth/setup"
    r = c.post(route, json={"password": password, "device_name": device_name})
    assert r.status_code == 200, r.text
    body = r.json()
    c.headers["Authorization"] = f"Bearer {body['access_token']}"
    return body


def mint_token(role: str, *, name: str | None = None, local_only: bool = True, expires_in_days: int | None = None) -> str:
    """A raw ``cai_`` API token of ``role``, created through the service as the system actor
    (the same path as ``python -m climate.cli create-token``)."""
    from climate import auth_service

    _row, raw = auth_service.create_api_token(
        auth_service.SYSTEM, name=name or f"pytest {role}", role=role, local_only=local_only,
        expires_in_days=expires_in_days,
    )
    return raw


@pytest.fixture
def db(fresh_db):
    from climate.store.db import _factory

    session = _factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(fresh_db):
    with make_client() as c:
        yield c


@pytest.fixture
def owner(fresh_db):
    with make_client() as c:
        sign_in(c)
        yield c


@pytest.fixture
def agent(fresh_db):
    with make_client(token=AGENT_TOKEN) as c:
        yield c


@pytest.fixture
def agent_token(fresh_db) -> str:
    return mint_token("agent")


@pytest.fixture
def viewer_token(fresh_db) -> str:
    return mint_token("viewer")


@pytest.fixture
def control_token(fresh_db) -> str:
    return mint_token("control")


@pytest.fixture
def viewer(viewer_token):
    with make_client(token=viewer_token) as c:
        yield c


@pytest.fixture
def control(control_token):
    with make_client(token=control_token) as c:
        yield c

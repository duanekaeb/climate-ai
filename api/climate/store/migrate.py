"""Tiny SQL migration runner.

Files in ``climate/store/migrations/NNN_name.sql`` run in name order, each in its own
transaction, and are recorded in ``schema_migrations``. A Postgres advisory lock makes
concurrent starts (api + worker) safe.
"""

from __future__ import annotations

import logging
from importlib import resources

from sqlalchemy import Engine, text

from climate.store.db import get_engine

log = logging.getLogger(__name__)
_LOCK_ID = 727_001  # arbitrary, stable


def _migration_files() -> list[tuple[str, str]]:
    pkg = resources.files("climate.store") / "migrations"
    files = sorted(p for p in pkg.iterdir() if p.name.endswith(".sql"))
    return [(p.name.removesuffix(".sql"), p.read_text(encoding="utf-8")) for p in files]


def migrate(engine: Engine | None = None) -> list[str]:
    """Apply pending migrations. Returns the ids applied this call."""
    engine = engine or get_engine()
    applied: list[str] = []
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _LOCK_ID})
        try:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    " id TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            conn.commit()
            done = {r[0] for r in conn.execute(text("SELECT id FROM schema_migrations"))}
            conn.commit()
            for mid, sql in _migration_files():
                if mid in done:
                    continue
                log.info("applying migration %s", mid)
                with conn.begin():
                    conn.exec_driver_sql(sql)
                    conn.execute(text("INSERT INTO schema_migrations (id) VALUES (:id)"), {"id": mid})
                applied.append(mid)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
            conn.commit()
    return applied

"""Database migration command helper."""

from __future__ import annotations

import psycopg

import kdive.config as config
from kdive.config.core_settings import DATABASE_URL
from kdive.db.migrate import apply_migrations


def migrate(database_url: str | None = None) -> int:
    """Apply database migrations only (ADR-0121).

    Inventory reconcile is the reconciler loop's job (ADR-0112) and the build-config seed is the
    ``seed-build-configs`` command (ADR-0096) — both are deliberately *not* run here, so a failed
    "migrate" Job always means a SQL migration failed, never a config/bucket fault.

    Args:
        database_url: A psycopg connection string, or ``None`` to read ``KDIVE_DATABASE_URL``.

    Returns:
        The number of migrations applied.
    """
    url = database_url or config.require(DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print(f"applied {len(applied)} migration(s)")
    return len(applied)

"""Shared fixtures for the process-runtime tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the runtime's
pool-warming contract (#1535) is asserted against a real backend rather than a fake that
would have to re-implement ``AsyncConnectionPool``'s open semantics to be meaningful.
"""

from __future__ import annotations

from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url

__all__ = ["_migrated_db", "migrated_url", "pg_conn", "postgres_url"]

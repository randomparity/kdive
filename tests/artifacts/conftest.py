"""Shared fixtures for the artifacts tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the artifacts suite
runs against the same per-test migrated schema (testcontainers Postgres), mirroring
``tests/jobs/conftest.py``. Needed by ``test_etag_repair``, whose subject writes a real
``artifacts`` row update.
"""

from __future__ import annotations

from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url

__all__ = ["_migrated_db", "migrated_url", "pg_conn", "postgres_url"]

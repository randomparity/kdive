"""Shared fixtures for the prereqs tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so this suite's
DB-backed tests run against the same per-test migrated schema (testcontainers Postgres).
"""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]

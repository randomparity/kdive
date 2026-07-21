"""Shared fixtures for the images tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the catalog
resolver and seed tests run against the same per-test migrated schema.
"""

from __future__ import annotations

from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url

__all__ = ["_migrated_db", "migrated_url", "pg_conn", "postgres_url"]

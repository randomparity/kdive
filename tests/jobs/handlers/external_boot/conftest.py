"""Fixtures for the external-boot handler package tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` the same way
``tests/jobs/conftest.py`` does, so a Postgres test in this package inherits ``migrated_url`` with
no extra wiring. The builders themselves live in ``support.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kdive.domain.operations.jobs import Job
from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url
from tests.jobs.handlers.external_boot.support import marked_job

__all__ = ["_migrated_db", "migrated_url", "pg_conn", "postgres_url"]


@pytest.fixture
def make_marked_job() -> Callable[..., Job]:
    return marked_job

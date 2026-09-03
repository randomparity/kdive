"""Fixtures for the external-boot handler package tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` the same way
``tests/jobs/conftest.py`` does, so a Postgres test in this package inherits ``migrated_url`` with
no extra wiring, and re-exports ``authority_role_dsns`` — the only route to a real LOGIN role.
That matters: everything otherwise connects as the backend **superuser**, and ``pg_has_role`` is
true for a superuser against every role, so a test meaning to prove a privilege boundary proves
nothing unless it connects through a real LOGIN principal.

The builders themselves live in ``support.py``, ``vehicle.py`` and ``seeding.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest
from psycopg import AsyncConnection

from kdive.domain.operations.jobs import Job
from kdive.providers.core.resolver import ProviderResolver
from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url
from tests.db.external_boot_authority_support import authority_role_dsns
from tests.jobs.handlers.external_boot.support import marked_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.systems_support import provider_resolver

__all__ = [
    "_migrated_db",
    "authority_role_dsns",
    "migrated_url",
    "pg_conn",
    "postgres_url",
]


@pytest.fixture
def make_marked_job() -> Callable[..., Job]:
    return marked_job


@pytest.fixture
def vehicle() -> Vehicle:
    """One fault-inject port driven through materialize/prepare, with ids minted first."""
    return build_vehicle()


def resolver_for(vehicle: Vehicle) -> ProviderResolver:
    """Bind the vehicle's port under ``ResourceKind.LOCAL_LIBVIRT``.

    The fault-inject *port* without the fault-inject *kind*: the marker's ``provider_kind`` admits
    only ``local-libvirt``/``remote-libvirt``, and ``allocate_external_boot_authority`` further
    requires ``v_run.target_kind = p_provider_kind``.
    """
    return provider_resolver(external_boot=vehicle.port)


async def role_connection(dsn: str) -> AsyncConnection:
    """Open an autocommit connection as one of the migration's LOGIN principals."""
    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)

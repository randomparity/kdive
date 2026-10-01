"""#3017: systems.teardown routes a preparing activation whose activate job allocated no authority.

The activate job's marker is the durable route (migration 0167, ADR-0620 amendment).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.jobs import cancel_job
from kdive.mcp.tools.lifecycle.runs.steps import boot_run
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.security.authz.rbac import Role
from tests.integration.external_boot_support import (
    PreparingProvider,
    configure_external_boot,
    fetch_one,
    seed_public_external_boot,
)
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver

_MARKER = "external_boot_authority_v1"


async def _boot(pool: AsyncConnectionPool, resolver: Any) -> tuple[str, str]:
    """Create the activation `preparing` through the public Run boot; return System and job."""
    run_id, system_id = await seed_public_external_boot(pool)
    admitted = await boot_run(pool, runs_support.ctx(), run_id, resolver=resolver)
    assert admitted.status == "queued", admitted.model_dump()
    return system_id, admitted.object_id


async def _teardown(pool: AsyncConnectionPool, system_id: str, resolver: Any) -> ToolResponse:
    return await teardown_system(pool, runs_support.ctx(Role.ADMIN), system_id, resolver=resolver)


@pytest.mark.parametrize("activate_job", ["canceled", "queued"])
def test_teardown_routes_by_the_activate_marker(migrated_url: str, activate_job: str) -> None:
    async def body() -> None:
        configure_external_boot()
        resolver = provider_resolver(external_boot=PreparingProvider())
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            system_id, job_id = await _boot(pool, resolver)
            if activate_job == "canceled":
                canceled = await cancel_job(pool, runs_support.ctx(), job_id)
                assert canceled.status == "canceled", canceled.model_dump()
            response = await _teardown(pool, system_id, resolver)
            assert response.status == "queued", response.model_dump()
            async with pool.connection() as conn:
                markers = await fetch_one(
                    conn,
                    "SELECT a.payload -> %s AS activate, t.payload -> %s AS teardown "
                    "FROM jobs a, jobs t WHERE a.id = %s AND t.id = %s",
                    (_MARKER, _MARKER, UUID(job_id), UUID(response.object_id)),
                )
                reservation = await fetch_one(
                    conn,
                    "SELECT r.state FROM external_boot_reservations r "
                    "JOIN external_boot_activations e ON e.id = r.activation_id "
                    "WHERE e.system_id = %s",
                    (UUID(system_id),),
                )

        route = ("provider_kind", "authority_instance", "activation_id")
        teardown = markers["teardown"]
        assert {key: teardown[key] for key in route} == {
            key: markers["activate"][key] for key in route
        }
        assert (teardown["purpose"], teardown["operation"]) == ("teardown", "teardown")
        # Enqueueing credits nothing: only the authority's teardown receipt ends the reservation.
        assert reservation == {"state": "pending"}

    asyncio.run(body())

"""#3017: a public Run boot whose activate job never allocated authority, then systems.teardown."""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

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


def test_canceled_activate_before_authority_leaves_teardown_unrouted(migrated_url: str) -> None:
    async def body() -> None:
        configure_external_boot()
        resolver = provider_resolver(external_boot=PreparingProvider())
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            run_id, system_id = await seed_public_external_boot(pool)
            admitted = await boot_run(pool, runs_support.ctx(), run_id, resolver=resolver)
            assert admitted.status == "queued", admitted.model_dump()
            canceled = await cancel_job(pool, runs_support.ctx(), admitted.object_id)
            assert canceled.status == "canceled", canceled.model_dump()
            response = await teardown_system(
                pool, runs_support.ctx(Role.ADMIN), system_id, resolver=resolver
            )
            async with pool.connection() as conn:
                activation = await fetch_one(
                    conn,
                    "SELECT e.state, r.state AS reservation, "
                    "(SELECT count(*) FROM external_boot_authorities a "
                    " WHERE a.activation_id = e.id) AS authorities "
                    "FROM external_boot_activations e "
                    "JOIN external_boot_reservations r ON r.activation_id = e.id "
                    "WHERE e.system_id = %s",
                    (UUID(system_id),),
                )

        assert activation == {"state": "preparing", "reservation": "pending", "authorities": 0}
        assert response.status == "error", response.model_dump()
        assert response.data["reason"] == "external_boot_teardown_authority_unresolved"

    asyncio.run(body())

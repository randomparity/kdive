"""The stranded-allocation recovery, from the stranded shape to a released allocation (#3552).

Every step goes through the operator's tools and the real worker and authority code under the
real database roles; only the two daemons are faked (ADR-0816). The refusals cover a domain still
present on either daemon: on the authority's, settlement refuses; on the worker's, release refuses
until the worker's teardown has removed it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import LiteralString
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.allocations.lifecycle import release_allocation
from kdive.mcp.tools.ops.security import breakglass
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.allocation.release import SYSTEM_TEARDOWN_REQUIRED_REASON
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.stranded_activation_support import (
    _DOMAIN,
    _authority_service,
    _AuthorityDaemon,
    _fail_job,
    _run_job,
    _strand,
    _Stranded,
    _WorkerDaemon,
)
from tests.mcp.systems_support import FakeProvisioning, ctx, provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE

_ADMIN = RequestContext(
    principal="admin",
    agent_session="session",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)


class _Lab:
    """One stranded System, its two faked daemons, and the operator's tools against them."""

    def __init__(
        self,
        migrated_url: str,
        role_dsns: _RoleDsns,
        tmp_path: Path,
        *,
        authority: _AuthorityDaemon,
        worker: _WorkerDaemon,
    ) -> None:
        self.url = migrated_url
        self.role_dsns = role_dsns
        self.stranded: _Stranded = _strand(migrated_url, role_dsns)
        self.case = self.stranded.case
        self.worker = worker
        self.service = _authority_service(
            tmp_path, role_dsns, self.case.authority_instance, authority
        )

    async def recover(self, pool: AsyncConnectionPool, step: str) -> ToolResponse:
        return await breakglass.recover_stranded_activation(
            pool, _ADMIN, activation_id=str(self.case.activation_id), step=step, reason="#3552"
        )

    async def observe(self, pool: AsyncConnectionPool) -> None:
        """Request the observation, then run it on the fixed worker as the worker commits it."""
        requested = await self.recover(pool, "observe")
        assert requested.status == "observation_requested"
        job_id = UUID(str(requested.data["job_id"]))
        async with pool.connection() as conn:
            job = await JOBS.get(conn, job_id)
        assert job is not None
        try:
            await _run_job(
                self.url, self.role_dsns, job, self.case.worker_id, self.worker, self.service
            )
        except CategorizedError as refused:
            _fail_job(self.url, job_id, refused)
            return
        with psycopg.connect(self.url) as conn:
            conn.execute(
                "UPDATE jobs SET state = 'succeeded', worker_id = NULL, lease_expires_at = NULL, "
                "heartbeat_at = NULL WHERE id = %s",
                (job_id,),
            )

    async def release(self, pool: AsyncConnectionPool) -> ToolResponse:
        return await release_allocation(pool, ctx(Role.ADMIN), str(self.case.allocation_id))

    def row(self, sql: LiteralString, key: UUID) -> object:
        with psycopg.connect(self.url) as conn:
            found = conn.execute(sql, (key,)).fetchone()
        assert found is not None
        return found[0]

    def activation_state(self) -> object:
        return self.row(
            "SELECT state FROM external_boot_activations WHERE id = %s", self.case.activation_id
        )

    def allocation_state(self) -> object:
        return self.row("SELECT state FROM allocations WHERE id = %s", self.case.allocation_id)

    def system_state(self) -> object:
        return self.row("SELECT state FROM systems WHERE id = %s", self.case.system_id)


async def _worker_teardown(lab: _Lab, pool: AsyncConnectionPool) -> FakeProvisioning:
    """Run the teardown ``ops.force_teardown`` queued, on the worker that holds the domain."""
    async with pool.connection() as conn:
        job: Job | None = await queue.get_by_dedup_key(conn, f"{lab.case.system_id}:teardown")
    assert job is not None
    assert set(job.payload) == {"system_id"}, "a settled System's teardown is the ordinary one"
    provisioner = FakeProvisioning()
    async with await psycopg.AsyncConnection.connect(
        lab.role_dsns("kdive_worker"), autocommit=True
    ) as conn:
        await systems_handlers.teardown_handler(
            conn,
            job,
            resolver=provider_resolver(provisioner=provisioner),
            artifact_store=INERT_OBJECT_STORE,
        )
    return provisioner


def _server_pool(lab: _Lab) -> AsyncConnectionPool:
    return AsyncConnectionPool(lab.role_dsns("kdive_server"), min_size=1, max_size=2, open=False)


def test_a_stranded_allocation_is_recovered_to_released(
    migrated_url: str, authority_role_dsns: _RoleDsns, tmp_path: Path
) -> None:
    lab = _Lab(
        migrated_url,
        authority_role_dsns,
        tmp_path,
        authority=_AuthorityDaemon(),
        worker=_WorkerDaemon(),
    )

    async def body() -> None:
        async with _server_pool(lab) as pool:
            stranded = await lab.release(pool)
            assert stranded.data["reason"] == "external_boot_restricted"
            await lab.observe(pool)
            settled = await lab.recover(pool, "settle")
            assert settled.status == "settled"
            assert settled.data["system_id"] == str(lab.case.system_id)
            held = await lab.release(pool)
            # The worker's daemon still holds the domain, so release waits for its teardown.
            assert held.data["reason"] == SYSTEM_TEARDOWN_REQUIRED_REASON
            torn = await breakglass.force_teardown(
                pool, _ADMIN, system_id=str(lab.case.system_id), reason="#3552"
            )
            assert torn.status == "queued"
            provisioner = await _worker_teardown(lab, pool)
            assert provisioner.torn_down == [_DOMAIN]
            released = await lab.release(pool)
            assert released.status == "released"

    asyncio.run(body())

    assert lab.activation_state() == "abandoned"
    assert lab.system_state() == "torn_down"
    assert lab.allocation_state() == "released"


@pytest.mark.parametrize(
    ("authority", "worker", "reason"),
    [
        (_AuthorityDaemon(domain_absent=False), _WorkerDaemon(), "authority_domain_present"),
        (_AuthorityDaemon(), _WorkerDaemon(defined=False), "worker_domain_absent"),
    ],
    ids=["domain-on-the-authority", "domain-absent-from-the-worker"],
)
def test_a_domain_in_the_wrong_place_refuses_and_changes_nothing(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    tmp_path: Path,
    authority: _AuthorityDaemon,
    worker: _WorkerDaemon,
    reason: str,
) -> None:
    lab = _Lab(migrated_url, authority_role_dsns, tmp_path, authority=authority, worker=worker)

    async def body() -> None:
        async with _server_pool(lab) as pool:
            await lab.observe(pool)
            refused = await lab.recover(pool, "settle")
            assert (refused.status, refused.data["reason"]) == ("error", reason)
            still = await lab.release(pool)
            assert still.data["reason"] == "external_boot_restricted"

    asyncio.run(body())

    assert lab.activation_state() == "preparing"
    assert lab.system_state() == "ready"
    assert lab.allocation_state() == "active"

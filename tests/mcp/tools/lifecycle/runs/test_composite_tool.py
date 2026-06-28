"""`runs.build_install_boot` composite tool tests (ADR-0268, #866).

Tests the MCP admission handler directly (injected pool + ctx), mirroring the
``runs.build`` test patterns in ``tests/mcp/lifecycle/test_runs_tools.py``.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.components.references import ComponentKind
from kdive.components.validation import ComponentSourceCapabilities
from kdive.db.build_hosts import WORKER_LOCAL_ID
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.runs.composite import CompositeRunHandlers
from kdive.security.authz.rbac import AuthorizationError, Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)

# A server build profile with a warm-tree kernel_source_ref and no build_host
# (resolves to the seeded worker-local host, which is always available).
_VALID_BUILD: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config": {"kind": "local", "path": "/configs/kdump.config"},
}

_TEST_COMPONENT_SOURCES = ComponentSourceCapabilities(
    provider="test-provider",
    accepted_component_sources={ComponentKind.CONFIG: frozenset({"local"})},
)
_COMPOSITE_HANDLERS = CompositeRunHandlers(_TEST_COMPONENT_SOURCES)


def _ctx(role: Role = Role.OPERATOR) -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": role}
    )


@asynccontextmanager
async def _pool(url: str):  # type: ignore[return]
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _provisioning_profile() -> dict[str, Any]:
    """Minimal valid provisioning profile for a System row."""
    from kdive.profiles.provisioning import ProvisioningProfile

    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": "/img"}}},
        }
    ).model_dump(by_alias=True)


async def _seed_bound_run(
    pool: AsyncConnectionPool,
    *,
    state: RunState = RunState.CREATED,
    build_profile: dict[str, Any] | None = None,
) -> str:
    """Seed a bound Run (Investigation + Resource + Allocation + System + Run) and return its id."""
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="seeded",
                state=InvestigationState.OPEN,
            ),
        )
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.ACTIVE,
                requested_vcpus=None,
                requested_memory_gb=None,
                requested_disk_gb=None,
                pcie_claim=[],
                lease_expiry=None,
            ),
        )
        sys = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile=_provisioning_profile(),
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv.id,
                system_id=sys.id,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile=copy.deepcopy(_VALID_BUILD)
                if build_profile is None
                else build_profile,
            ),
        )
    return str(run.id)


# ---------------------------------------------------------------------------
# Admission + enqueue
# ---------------------------------------------------------------------------


def test_build_install_boot_enqueues_one_job_with_host_id(migrated_url: str) -> None:
    """Admission runs, exactly one build_install_boot job is enqueued with build_host_id."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_bound_run(pool, state=RunState.CREATED)
            resp = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), run_id)

            assert resp.status == "queued"
            assert resp.data["run_id"] == run_id
            assert resp.data["kind"] == "build_install_boot"
            assert "jobs.wait" in resp.suggested_next_actions

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT payload FROM jobs WHERE kind='build_install_boot' AND dedup_key=%s",
                    (f"{run_id}:build_install_boot",),
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind='build_install_boot'",
                )
                count_row = await cur.fetchone()

        assert row is not None, "expected one build_install_boot job"
        payload = row["payload"]
        assert payload["build_host_id"] == str(WORKER_LOCAL_ID)  # admission ran
        assert count_row is not None and count_row["n"] == 1

    asyncio.run(_run())


def test_build_install_boot_flips_run_to_running(migrated_url: str) -> None:
    """Enqueue transitions a CREATED run to RUNNING (same state machine as runs.build)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_bound_run(pool, state=RunState.CREATED)
            await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), run_id)

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (UUID(run_id),))
                row = await cur.fetchone()

        assert row is not None and row["state"] == "running"

    asyncio.run(_run())


def test_build_install_boot_is_idempotent(migrated_url: str) -> None:
    """A repeated call returns the same job envelope (dedup key prevents double-enqueue)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_bound_run(pool, state=RunState.CREATED)
            r1 = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), run_id)
            r2 = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), run_id)

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind='build_install_boot'",
                )
                row = await cur.fetchone()

        assert r1.object_id == r2.object_id
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_build_install_boot_requires_operator(migrated_url: str) -> None:
    """A CONTRIBUTOR or lower role raises AuthorizationError."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_bound_run(pool, state=RunState.CREATED)
            with pytest.raises(AuthorizationError):
                await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(Role.CONTRIBUTOR), run_id)

    asyncio.run(_run())


def test_build_install_boot_terminal_run_is_config_error(migrated_url: str) -> None:
    """A terminal (canceled/failed) Run returns a configuration_error."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_bound_run(pool, state=RunState.CANCELED)
            resp = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_build_install_boot_missing_run_is_config_error(migrated_url: str) -> None:
    """A non-existent run_id returns a configuration_error (not leaked as not_found)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), str(uuid4()))
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_build_install_boot_malformed_uuid_is_config_error(migrated_url: str) -> None:
    """A non-UUID run_id string returns a configuration_error."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_build_install_boot_rejected_when_build_job_live(migrated_url: str) -> None:
    """A live 'build' job for the run → composite returns configuration_error, not a raw DB error.

    Regression for I2: before this fix, runs.build_install_boot would fall through to
    resolve_and_admit → try_acquire_lease → UniqueViolation on the build_host_leases PK.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # Run is already RUNNING because runs.build flipped it.
            run_id = await _seed_bound_run(pool, state=RunState.RUNNING)
            # Seed a live standalone build job — simulates a prior runs.build call.
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, "
                    "    authorizing, dedup_key) "
                    "VALUES (%s, 'build', %s, 'queued', 1, 3, %s, %s)",
                    (
                        uuid4(),
                        Jsonb({"run_id": run_id}),
                        Jsonb({"principal": "user-1", "agent_session": None, "project": "proj"}),
                        f"build:{run_id}",
                    ),
                )
            resp = await _COMPOSITE_HANDLERS.build_install_boot(pool, _ctx(), run_id)

        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data.get("reason") == "build_already_in_progress"

    asyncio.run(_run())

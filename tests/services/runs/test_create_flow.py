"""PG-backed service tests for the runs.create admission flow (ADR-0169).

These exercise ``services.runs.admission.create_run`` directly (bound and unbound paths) and
pin the persisted Run/audit/investigation state, so the async locked create flow is
mutation-attributable without the MCP tool layer. The pure decision helpers are covered by
``test_admission_helpers.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, Investigation, System
from kdive.security.audit import args_digest
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.runs.admission import (
    RunCreateRequest,
    RunReuseRequirementInput,
    create_run,
)
from kdive.services.runs.host_admission import RunCreateError
from tests.db.conftest import migrated_url  # noqa: F401
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 6, 18)
_SIZING = {"vcpu": 2, "memory_mb": 4096, "disk_gb": 20}


def _ctx(*, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    return RequestContext(
        principal="user-1",
        agent_session="s",
        projects=projects,
        roles=dict.fromkeys(projects, Role.OPERATOR),
    )


async def _pool_open(url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    return pool


async def _seed_investigation(
    pool: AsyncConnectionPool,
    *,
    state: InvestigationState = InvestigationState.OPEN,
    project: str = "proj",
) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    alloc_state: AllocationState = AllocationState.ACTIVE,
    project: str = "proj",
    provisioning_profile: dict[str, Any] | None = None,
) -> str:
    async with pool.connection() as conn:
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
                project=project,
                resource_id=res.id,
                state=alloc_state,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile=provisioning_profile or {},
            ),
        )
    return str(system.id)


class _Recorder:
    """An idempotency recorder that captures the results it is handed inside the txn."""

    def __init__(self) -> None:
        self.results: list[Any] = []

    async def __call__(self, conn: object, result: Any) -> None:
        del conn
        self.results.append(result)


async def _create(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
    *,
    recorder: _Recorder | None = None,
):
    return await create_run(pool, ctx, request, resolver=provider_resolver(), recorder=recorder)


async def _fetchall(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _fetchone(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> tuple:
    rows = await _fetchall(pool, query, params)
    assert len(rows) == 1
    return rows[0]


def test_create_bound_run_persists_run_audit_and_flips_investigation(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            recorder = _Recorder()
            result = await _create(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id, system_id=sys_id, build_profile={"schema_version": 1}
                ),
                recorder=recorder,
            )
            run_row = await _fetchone(
                pool,
                "SELECT state, system_id, investigation_id, project, target_kind, label, "
                "agent_session FROM runs WHERE id = %s",
                (str(result.run_id),),
            )
            create_audit = await _fetchone(
                pool,
                "SELECT tool, object_kind, transition, args_digest FROM audit_log "
                "WHERE object_id = %s",
                (str(result.run_id),),
            )
            inv_audit = await _fetchall(
                pool,
                "SELECT tool, object_kind, transition, args_digest FROM audit_log "
                "WHERE object_id = %s ORDER BY ts",
                (inv_id,),
            )
            inv_state, last_run_at = await _fetchone(
                pool, "SELECT state, last_run_at FROM investigations WHERE id = %s", (inv_id,)
            )
        finally:
            await pool.close()

        assert result.project == "proj"
        assert result.investigation_id == UUID(inv_id)
        assert result.system_id == UUID(sys_id)
        assert result.target_kind == ResourceKind.LOCAL_LIBVIRT
        assert result.label is None
        assert result.expected_boot_failure_kind is None
        assert run_row == (
            RunState.CREATED.value,
            UUID(sys_id),
            UUID(inv_id),
            "proj",
            ResourceKind.LOCAL_LIBVIRT.value,
            None,
            "s",
        )
        assert [r.run_id for r in recorder.results] == [result.run_id]
        assert create_audit == (
            "runs.create",
            "runs",
            "->created",
            args_digest({"investigation_id": inv_id, "system_id": sys_id}),
        )
        assert inv_audit == [
            (
                "runs.create",
                "investigations",
                "open->active",
                args_digest({"investigation_id": inv_id}),
            )
        ]
        assert inv_state == InvestigationState.ACTIVE.value
        assert last_run_at is not None

    asyncio.run(_run())


def test_create_bound_echoes_label_and_expected_boot_failure(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool, state=InvestigationState.ACTIVE)
            sys_id = await _seed_system(pool)
            result = await _create(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                    label="repro-A",
                    expected_boot_failure={"kind": "panic"},
                ),
            )
            label, ebf = await _fetchone(
                pool,
                "SELECT label, expected_boot_failure FROM runs WHERE id = %s",
                (str(result.run_id),),
            )
            inv_audit_count = await _fetchone(
                pool, "SELECT count(*) FROM audit_log WHERE object_id = %s", (inv_id,)
            )
        finally:
            await pool.close()

        assert result.label == "repro-A"
        assert result.expected_boot_failure_kind == "panic"
        assert label == "repro-A"
        assert ebf["kind"] == "panic"
        # An already-ACTIVE investigation is not flipped, so no open->active audit is written.
        assert inv_audit_count[0] == 0

    asyncio.run(_run())


def test_create_unbound_run_holds_no_system(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            recorder = _Recorder()
            result = await _create(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=None,
                    target_kind=ResourceKind.LOCAL_LIBVIRT.value,
                    build_profile={"schema_version": 1},
                    label="u-label",
                    expected_boot_failure={"kind": "panic"},
                ),
                recorder=recorder,
            )
            system_id, run_label, run_ebf = await _fetchone(
                pool,
                "SELECT system_id, label, expected_boot_failure FROM runs WHERE id = %s",
                (str(result.run_id),),
            )
            audit = await _fetchone(
                pool,
                "SELECT args_digest FROM audit_log WHERE object_id = %s AND object_kind = 'runs'",
                (str(result.run_id),),
            )
        finally:
            await pool.close()

        assert result.system_id is None
        assert result.target_kind == ResourceKind.LOCAL_LIBVIRT
        assert result.project == "proj"
        assert result.label == "u-label"
        assert result.expected_boot_failure_kind == "panic"
        assert system_id is None
        assert run_label == "u-label"
        assert run_ebf["kind"] == "panic"
        assert [r.run_id for r in recorder.results] == [result.run_id]
        assert audit[0] == args_digest(
            {"investigation_id": inv_id, "target_kind": ResourceKind.LOCAL_LIBVIRT.value}
        )

    asyncio.run(_run())


async def _expect_reject(pool: AsyncConnectionPool, request: RunCreateRequest) -> RunCreateError:
    try:
        await _create(pool, _ctx(), request)
    except RunCreateError as exc:
        return exc
    raise AssertionError("create_run did not raise RunCreateError")


def test_create_missing_investigation_is_config_error(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            sys_id = await _seed_system(pool)
            missing_inv = str(uuid4())
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=missing_inv,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == missing_inv

    asyncio.run(_run())


def test_create_missing_system_is_config_error(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            missing_sys = str(uuid4())
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=missing_sys,
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == missing_sys

    asyncio.run(_run())


def test_create_non_hostable_allocation_is_stale(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, alloc_state=AllocationState.RELEASING)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.STALE_HANDLE
        assert exc.object_id == sys_id
        assert exc.details == {"current_status": AllocationState.RELEASING.value}

    asyncio.run(_run())


def test_create_closed_investigation_is_config_error(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool, state=InvestigationState.CLOSED)
            sys_id = await _seed_system(pool)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == inv_id
        assert exc.details == {"current_status": InvestigationState.CLOSED.value}

    asyncio.run(_run())


def test_create_bound_target_kind_mismatch_is_rejected(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    target_kind="remote-libvirt",
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == sys_id
        assert exc.details is not None
        assert exc.details["reason"] == "target_kind_mismatch"
        assert exc.details["system_kind"] == ResourceKind.LOCAL_LIBVIRT.value
        assert exc.details["target_kind"] == "remote-libvirt"

    asyncio.run(_run())


def test_create_unbound_unknown_target_kind_is_rejected(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=None,
                    target_kind="not-a-real-kind",
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == inv_id
        assert exc.details == {"reason": "unknown_target_kind"}

    asyncio.run(_run())


def test_create_unbound_reuse_requirement_requires_system(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=None,
                    target_kind=ResourceKind.LOCAL_LIBVIRT.value,
                    build_profile={"schema_version": 1},
                    reuse_requirement=RunReuseRequirementInput(vcpus=2),
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == inv_id
        assert exc.details == {"reason": "reuse_requires_system"}

    asyncio.run(_run())


def test_create_bound_reuse_requirement_unmet_is_rejected(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, provisioning_profile=_SIZING)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                    reuse_requirement=RunReuseRequirementInput(vcpus=999),
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.details == {"reason": "reuse_requirement_unmet"}

    asyncio.run(_run())


def test_create_invalid_reuse_sizing_keys_error_on_object(migrated_url: str) -> None:  # noqa: F811
    # An early (pre-lock) reuse-sizing rejection still keys its envelope on the bound object id.
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                    reuse_requirement=RunReuseRequirementInput(vcpus=0),
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == sys_id

    asyncio.run(_run())


def test_create_bad_expected_boot_failure_keys_error_on_object(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                    expected_boot_failure={"kind": "not-a-real-kind"},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == sys_id
        assert exc.details == {"reason": "bad_expected_boot_failure"}

    asyncio.run(_run())


def test_create_unbound_missing_investigation_is_config_error(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            missing_inv = str(uuid4())
            exc = await _expect_reject(
                pool,
                RunCreateRequest(
                    investigation_id=missing_inv,
                    system_id=None,
                    target_kind=ResourceKind.LOCAL_LIBVIRT.value,
                    build_profile={"schema_version": 1},
                ),
            )
        finally:
            await pool.close()
        assert exc.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.object_id == missing_inv

    asyncio.run(_run())


def test_reuse_requirement_rejects_non_positive_sizing() -> None:
    with pytest.raises(CategorizedError) as exc:
        RunReuseRequirementInput(vcpus=0).to_domain()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "vcpus"}


def test_reuse_requirement_accepts_positive_sizing() -> None:
    domain = RunReuseRequirementInput(vcpus=2, memory_gb=4, disk_gb=20, pcie=["10de:1"]).to_domain()
    assert domain.vcpus == 2
    assert domain.memory_gb == 4
    assert domain.disk_gb == 20
    assert domain.pcie == ["10de:1"]

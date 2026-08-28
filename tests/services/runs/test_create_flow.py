"""PG-backed service tests for the runs.create admission flow (ADR-0169).

These exercise ``services.runs.admission.create_run`` directly (bound and unbound paths) and
pin the persisted Run/audit/investigation state, so the async locked create flow is
mutation-attributable without the MCP tool layer. The pure decision helpers are covered by
``test_admission_helpers.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
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
from kdive.reconciler.cleanup.artifacts.artifact_retention import gc_expired_build_artifacts
from kdive.security.audit import args_digest
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.runs.admission import (
    RunCreateRequest,
    RunReuseRequirementInput,
    create_run,
)
from kdive.services.runs.bind import RunBindRequest, bind_run
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
    return await create_run(
        pool,
        ctx,
        request,
        available_target_kinds=provider_resolver().registered_kinds(),
        recorder=recorder,
    )


async def _fetchall(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _fetchone(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> tuple:
    rows = await _fetchall(pool, query, params)
    assert len(rows) == 1
    return rows[0]


async def _seed_reusable_build(
    pool: AsyncConnectionPool,
    investigation_id: str,
    *,
    profile: dict[str, Any] | None = None,
    state: str = "active",
    expired: bool = False,
    target_kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
) -> str:
    digest = "a" * 64
    generation = uuid4()
    build_ref = f"{digest}.{generation}"
    result = {
        "kernel_ref": "investigations/kernel",
        "debuginfo_ref": "investigations/vmlinux",
        "build_id": "build-1",
        "cmdline": "console=ttyS0",
    }
    expires_at = datetime.now(UTC) + (timedelta(days=-1) if expired else timedelta(days=7))
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO investigation_builds "
            "(investigation_id, generation, build_ref, content_digest, canonical_document, "
            "build_result, artifacts, target_kind, build_profile, state, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                investigation_id,
                generation,
                build_ref,
                digest,
                Jsonb({"version": 1}),
                Jsonb(result),
                Jsonb(
                    {
                        "kernel": {
                            "key": "investigations/kernel",
                            "version_id": "kernel-v1",
                        },
                        "vmlinux": {
                            "key": "investigations/vmlinux",
                            "version_id": "vmlinux-v1",
                        },
                    }
                ),
                target_kind.value,
                Jsonb(profile or {"schema_version": 1, "arch": "x86_64"}),
                state,
                expires_at,
            ),
        )
    return build_ref


def test_create_bound_run_reuses_investigation_build(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            build_ref = await _seed_reusable_build(pool, inv_id)
            result = await _create(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={"schema_version": 1},
                    build_ref=build_ref,
                ),
            )
            assert result.build_ref == build_ref
            run = await _fetchone(
                pool,
                "SELECT state, kernel_ref, debuginfo_ref, build_ref FROM runs WHERE id = %s",
                (result.run_id,),
            )
            assert run == (
                "succeeded",
                "investigations/kernel",
                "investigations/vmlinux",
                build_ref,
            )
            step = await _fetchone(
                pool,
                "SELECT state, result FROM run_steps WHERE run_id = %s AND step = 'build'",
                (result.run_id,),
            )
            assert step[0] == "succeeded"
            assert step[1]["build_ref"] == build_ref
            assert step[1]["expires_at"] == result.build_expires_at
            assert step[1]["artifact_versions"] == {
                "kernel": "kernel-v1",
                "vmlinux": "vmlinux-v1",
            }
        finally:
            await pool.close()

    asyncio.run(_run())


def test_reusable_build_serves_two_systems_and_unbound_bind(migrated_url: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            systems = [await _seed_system(pool) for _ in range(3)]
            build_ref = await _seed_reusable_build(pool, inv_id)
            runs = []
            for system_id in systems[:2]:
                runs.append(
                    await _create(
                        pool,
                        _ctx(),
                        RunCreateRequest(
                            investigation_id=inv_id,
                            system_id=system_id,
                            build_profile={"schema_version": 1},
                            build_ref=build_ref,
                        ),
                    )
                )
            unbound = await _create(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    target_kind="local-libvirt",
                    build_profile={"schema_version": 1},
                    build_ref=build_ref,
                ),
            )
            bound = await bind_run(
                pool,
                _ctx(),
                RunBindRequest(run_id=str(unbound.run_id), system_id=systems[2]),
            )
            assert str(bound.system_id) == systems[2]
            step_results = await _fetchall(
                pool,
                "SELECT result FROM run_steps WHERE run_id = ANY(%s) AND step = 'build' "
                "ORDER BY result::text",
                ([run.run_id for run in [*runs, unbound]],),
            )
            assert len(step_results) == 3
            assert step_results[0] == step_results[1] == step_results[2]
            assert step_results[0][0] == {
                "kernel_ref": "investigations/kernel",
                "debuginfo_ref": "investigations/vmlinux",
                "build_id": "build-1",
                "cmdline": "console=ttyS0",
                "build_ref": build_ref,
                "expires_at": runs[0].build_expires_at,
                "artifact_versions": {
                    "kernel": "kernel-v1",
                    "vmlinux": "vmlinux-v1",
                },
            }
            manifests = await _fetchall(
                pool,
                "SELECT owner_id FROM upload_manifests WHERE owner_kind = 'runs' "
                "AND owner_id = ANY(%s)",
                ([run.run_id for run in [*runs, unbound]],),
            )
            assert manifests == []
        finally:
            await pool.close()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "mode",
    ["malformed", "missing", "cross_investigation", "target", "profile", "expired", "reclaiming"],
)
def test_create_rejects_unusable_reusable_build(migrated_url: str, mode: str) -> None:  # noqa: F811
    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            owner_id = await _seed_investigation(pool) if mode == "cross_investigation" else inv_id
            build_ref = (
                "not-a-build-ref"
                if mode == "malformed"
                else f"{'f' * 64}.{uuid4()}"
                if mode == "missing"
                else await _seed_reusable_build(
                    pool,
                    owner_id,
                    profile=(
                        {"schema_version": 1, "arch": "ppc64le"} if mode == "profile" else None
                    ),
                    state="reclaiming" if mode == "reclaiming" else "active",
                    expired=mode in {"expired", "reclaiming"},
                    target_kind=(
                        ResourceKind.FAULT_INJECT
                        if mode == "target"
                        else ResourceKind.LOCAL_LIBVIRT
                    ),
                )
            )
            with pytest.raises(RunCreateError) as caught:
                await _create(
                    pool,
                    _ctx(),
                    RunCreateRequest(
                        investigation_id=inv_id,
                        system_id=sys_id,
                        build_profile={"schema_version": 1},
                        build_ref=build_ref,
                    ),
                )
            expected = {
                "cross_investigation": "build_ref_not_found",
                "malformed": "build_ref_not_found",
                "missing": "build_ref_not_found",
                "target": "build_ref_incompatible",
                "profile": "build_ref_incompatible",
                "expired": "build_ref_expired",
                "reclaiming": "build_ref_expired",
            }[mode]
            assert caught.value.details["reason"] == expected
            if mode == "reclaiming":
                assert caught.value.details["expires_at"]
                assert caught.value.details["server_time"]
            rows = await _fetchall(
                pool, "SELECT id FROM runs WHERE investigation_id = %s", (inv_id,)
            )
            assert rows == []
            inv_state, last_run_at = await _fetchone(
                pool,
                "SELECT state, last_run_at FROM investigations WHERE id = %s",
                (inv_id,),
            )
            assert inv_state == "open"
            assert last_run_at is None
            assert (
                await _fetchall(
                    pool, "SELECT object_id FROM audit_log WHERE object_id = %s", (inv_id,)
                )
                == []
            )
            assert (
                await _fetchall(pool, "SELECT id FROM runs WHERE system_id = %s", (sys_id,)) == []
            )
        finally:
            await pool.close()

    asyncio.run(_run())


def test_create_reports_expired_after_generation_was_reclaimed(migrated_url: str) -> None:  # noqa: F811
    class _Store:
        def delete_version(self, key: str, version_id: str) -> None:
            del key, version_id

        def delete_retired_key_batch(self, key: str, limit: int) -> bool:
            del key, limit
            return True

    async def _run() -> None:
        pool = await _pool_open(migrated_url)
        try:
            investigation_id = await _seed_investigation(pool)
            system_id = await _seed_system(pool)
            build_ref = await _seed_reusable_build(pool, investigation_id, expired=True)
            async with pool.connection() as conn:
                await gc_expired_build_artifacts(conn, _Store(), timedelta(days=30))

            with pytest.raises(RunCreateError) as caught:
                await _create(
                    pool,
                    _ctx(),
                    RunCreateRequest(
                        investigation_id=investigation_id,
                        system_id=system_id,
                        build_profile={"schema_version": 1},
                        build_ref=build_ref,
                    ),
                )
            assert caught.value.details["reason"] == "build_ref_expired"
            assert caught.value.details["expires_at"]
            assert caught.value.details["server_time"]

            other_investigation = await _seed_investigation(pool)
            with pytest.raises(RunCreateError) as cross_tenant:
                await _create(
                    pool,
                    _ctx(),
                    RunCreateRequest(
                        investigation_id=other_investigation,
                        system_id=system_id,
                        build_profile={"schema_version": 1},
                        build_ref=build_ref,
                    ),
                )
            assert cross_tenant.value.details == {"reason": "build_ref_not_found"}
        finally:
            await pool.close()

    asyncio.run(_run())


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

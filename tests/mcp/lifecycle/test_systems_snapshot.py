"""Admission tests for the snapshot/restore `systems.*` tools (#1254, ADR-0378).

Drives the handlers through ``with_runtime_for_system`` (the exact registrar wiring, so role +
capability + membership are exercised) against a migrated Postgres with a fake provider runtime —
the worker never runs, so only the synchronous admission + enqueue is under test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    DEBUG_SESSIONS,
    INVESTIGATIONS,
    RESOURCES,
    RUNS,
    SNAPSHOTS,
    SYSTEMS,
    snapshot_by_name,
)
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SnapshotState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import (
    Allocation,
    DebugSession,
    Investigation,
    Run,
    Snapshot,
    System,
)
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import SnapshotPayload
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._runtime_resolution import with_runtime_for_system
from kdive.mcp.tools.lifecycle.systems.snapshot import (
    delete_snapshot,
    list_snapshots,
    restore_system,
    snapshot_system,
)
from kdive.mcp.tools.lifecycle.systems.view import get_system
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, RoleDenied
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 7, 17, tzinfo=UTC)


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=2, open=False)


async def _seed_system(pool: AsyncConnectionPool, state: SystemState) -> UUID:
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
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=state,
                provisioning_profile={},
                domain_name="kdive-x",
            ),
        )
    return system.id


async def _seed_snapshot(
    pool: AsyncConnectionPool,
    system_id: UUID,
    name: str,
    state: SnapshotState,
    *,
    include_memory: bool = True,
) -> UUID:
    async with pool.connection() as conn:
        row = await SNAPSHOTS.insert(
            conn,
            Snapshot(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                system_id=system_id,
                name=name,
                include_memory=include_memory,
                state=state,
            ),
        )
    return row.id


async def _seed_run(pool: AsyncConnectionPool, system_id: UUID, state: RunState) -> UUID:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="t",
                state=InvestigationState.ACTIVE,
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
                system_id=system_id,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile={},
            ),
        )
    return run.id


async def _seed_debug_session(pool: AsyncConnectionPool, run_id: UUID) -> None:
    async with pool.connection() as conn:
        await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                run_id=run_id,
                state=DebugSessionState.LIVE,
                transport="gdbstub",
                transport_handle="gdbstub://127.0.0.1:1234",
            ),
        )


async def _snapshot(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    resolver: ProviderResolver,
    sid: UUID,
    name: str,
    *,
    include_memory: bool = True,
) -> ToolResponse:
    return await with_runtime_for_system(
        pool,
        resolver,
        ctx,
        str(sid),
        lambda runtime: snapshot_system(
            pool, ctx, runtime, system_id=str(sid), name=name, include_memory=include_memory
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _restore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    resolver: ProviderResolver,
    sid: UUID,
    name: str,
    *,
    start_paused: bool = False,
) -> ToolResponse:
    return await with_runtime_for_system(
        pool,
        resolver,
        ctx,
        str(sid),
        lambda runtime: restore_system(
            pool, ctx, runtime, system_id=str(sid), name=name, start_paused=start_paused
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _list(
    pool: AsyncConnectionPool, ctx: RequestContext, resolver: ProviderResolver, sid: UUID
) -> ToolResponse:
    return await with_runtime_for_system(
        pool,
        resolver,
        ctx,
        str(sid),
        lambda runtime: list_snapshots(pool, ctx, runtime, system_id=str(sid)),
        required_role=Role.VIEWER,
    )


async def _delete(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    resolver: ProviderResolver,
    sid: UUID,
    name: str,
) -> ToolResponse:
    return await with_runtime_for_system(
        pool,
        resolver,
        ctx,
        str(sid),
        lambda runtime: delete_snapshot(pool, ctx, runtime, system_id=str(sid), name=name),
        required_role=Role.CONTRIBUTOR,
    )


async def _job_count(pool: AsyncConnectionPool, system_id: UUID, kind: JobKind) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM jobs WHERE payload->>'system_id' = %s AND kind = %s",
            (str(system_id), kind.value),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


# --- systems.snapshot -----------------------------------------------------------------------


def test_snapshot_on_ready_inserts_creating_row_and_enqueues_job(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            resolver = provider_resolver()
            resp = await _snapshot(pool, _ctx(), resolver, sid, "before-bug")
            assert resp.status == "queued"
            assert resp.data["system_id"] == str(sid)
            async with pool.connection() as conn:
                row = await snapshot_by_name(conn, sid, "before-bug")
                assert row is not None and row.state is SnapshotState.CREATING
                sys_row = await SYSTEMS.get(conn, sid)
                assert sys_row is not None and sys_row.state is SystemState.READY
            assert await _job_count(pool, sid, JobKind.SNAPSHOT) == 1
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_admitted_during_a_live_run(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_run(pool, sid, RunState.RUNNING)
            resp = await _snapshot(pool, _ctx(), provider_resolver(), sid, "mid-debug")
            assert resp.status == "queued"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_rejects_a_non_ready_system(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.REPROVISIONING)
            resp = await _snapshot(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["current_status"] == "reprovisioning"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_rejects_an_invalid_name(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            resp = await _snapshot(pool, _ctx(), provider_resolver(), sid, "bad name!")
            assert resp.status == "error"
            assert resp.data["reason"] == "invalid_snapshot_name"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_over_available_name_is_rejected(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            resp = await _snapshot(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "snapshot_name_in_use"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_over_failed_name_auto_reclaims(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            stale = await _seed_snapshot(pool, sid, "cp", SnapshotState.FAILED)
            resp = await _snapshot(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "queued"
            async with pool.connection() as conn:
                row = await snapshot_by_name(conn, sid, "cp")
                assert row is not None and row.id != stale  # a fresh creating row replaced it
                assert row.state is SnapshotState.CREATING
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_over_stale_creating_name_replaces_it(migrated_url: str) -> None:
    # A `creating` row with no live SNAPSHOT job (worker died) is stale: admission deletes it and
    # creates fresh rather than wedging the name forever.
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            stale = await _seed_snapshot(pool, sid, "cp", SnapshotState.CREATING)
            resp = await _snapshot(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "queued"
            async with pool.connection() as conn:
                row = await snapshot_by_name(conn, sid, "cp")
                assert row is not None and row.id != stale
        finally:
            await pool.close()

    asyncio.run(scenario())


# --- systems.restore ------------------------------------------------------------------------


def test_restore_transitions_ready_to_restoring_and_enqueues_job(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            resp = await _restore(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "queued"
            async with pool.connection() as conn:
                sys_row = await SYSTEMS.get(conn, sid)
                assert sys_row is not None and sys_row.state is SystemState.RESTORING
            assert await _job_count(pool, sid, JobKind.RESTORE) == 1
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_rejects_unknown_or_non_available_snapshot(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.CREATING)
            resp = await _restore(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "snapshot_not_available"
            async with pool.connection() as conn:
                sys_row = await SYSTEMS.get(conn, sid)
                assert sys_row is not None and sys_row.state is SystemState.READY
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_paused_against_disk_only_is_rejected(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE, include_memory=False)
            resp = await _restore(pool, _ctx(), provider_resolver(), sid, "cp", start_paused=True)
            assert resp.status == "error"
            assert resp.data["reason"] == "disk_only_no_pause"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_rejects_a_live_run(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            await _seed_run(pool, sid, RunState.RUNNING)
            resp = await _restore(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "live_run"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_rejects_an_in_flight_snapshot_op(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            snap_id = await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            async with pool.connection() as conn:
                await queue.enqueue(
                    conn,
                    JobKind.SNAPSHOT,
                    SnapshotPayload(
                        system_id=str(sid),
                        snapshot_id=str(snap_id),
                        name="other",
                        include_memory=True,
                    ),
                    {"principal": "user-1", "agent_session": None, "project": "proj"},
                    f"{sid}:snapshot:other",
                )
            resp = await _restore(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "snapshot_op_in_progress"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_rejects_an_attached_debug_session(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            run_id = await _seed_run(pool, sid, RunState.SUCCEEDED)
            await _seed_debug_session(pool, run_id)
            resp = await _restore(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "debug_session_attached"
        finally:
            await pool.close()

    asyncio.run(scenario())


# --- systems.list_snapshots -----------------------------------------------------------------


def test_list_snapshots_returns_rows_newest_first(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "older", SnapshotState.AVAILABLE)
            await _seed_snapshot(pool, sid, "newer", SnapshotState.FAILED)
            resp = await _list(pool, _ctx(), provider_resolver(), sid)
            assert resp.status == "ok"
            names = [item.data["name"] for item in resp.items]
            assert set(names) == {"older", "newer"}
            states = {item.data["name"]: item.data["state"] for item in resp.items}
            assert states["newer"] == "failed"  # a failed row still lists, not a failure envelope
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_list_snapshots_empty_for_none(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            resp = await _list(pool, _ctx(), provider_resolver(), sid)
            assert resp.status == "ok"
            assert resp.data["count"] == 0
        finally:
            await pool.close()

    asyncio.run(scenario())


# --- systems.delete_snapshot ----------------------------------------------------------------


def test_delete_snapshot_enqueues_a_delete_job(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            resp = await _delete(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "queued"
            assert await _job_count(pool, sid, JobKind.DELETE_SNAPSHOT) == 1
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_delete_snapshot_rejects_creating(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.CREATING)
            resp = await _delete(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "snapshot_creating"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_delete_snapshot_rejected_while_restoring(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.RESTORING)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            resp = await _delete(pool, _ctx(), provider_resolver(), sid, "cp")
            assert resp.status == "error"
            assert resp.data["reason"] == "system_restoring"
        finally:
            await pool.close()

    asyncio.run(scenario())


# --- capability + get + RBAC ----------------------------------------------------------------


def test_all_tools_refuse_a_provider_without_snapshot_support(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            resolver = provider_resolver(supports_snapshots=False)
            for resp in (
                await _snapshot(pool, _ctx(), resolver, sid, "cp2"),
                await _restore(pool, _ctx(), resolver, sid, "cp"),
                await _list(pool, _ctx(), resolver, sid),
                await _delete(pool, _ctx(), resolver, sid, "cp"),
            ):
                assert resp.status == "error"
                assert resp.data["reason"] == "capability_unsupported"
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_systems_get_surfaces_supports_snapshots(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            supported = await get_system(
                pool, _ctx(), str(sid), resolver=provider_resolver(supports_snapshots=True)
            )
            assert supported.data["supports_snapshots"] is True
            unsupported = await get_system(
                pool, _ctx(), str(sid), resolver=provider_resolver(supports_snapshots=False)
            )
            assert unsupported.data["supports_snapshots"] is False
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_systems_get_surfaces_supports_traffic_capture(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            supported = await get_system(
                pool, _ctx(), str(sid), resolver=provider_resolver(supports_traffic_capture=True)
            )
            assert supported.data["supports_traffic_capture"] is True
            unsupported = await get_system(
                pool, _ctx(), str(sid), resolver=provider_resolver(supports_traffic_capture=False)
            )
            assert unsupported.data["supports_traffic_capture"] is False
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_systems_get_resilient_when_provider_unregistered(migrated_url: str) -> None:
    # A System whose provider kind is no longer registered (disabled/uncomposed) cannot resolve a
    # runtime; systems.get must still return the System envelope (so an agent can read state and
    # tear it down), omitting supports_snapshots rather than failing as a raw error.
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            resp = await get_system(pool, _ctx(), str(sid), resolver=ProviderResolver({}))
            assert resp.status == "ready"
            assert "supports_snapshots" not in resp.data
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_viewer_is_denied_the_mutating_snapshot_tools(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            resolver = provider_resolver()
            with pytest.raises(RoleDenied):
                await _snapshot(pool, _ctx(role=Role.VIEWER), resolver, sid, "cp")
            with pytest.raises(RoleDenied):
                await _restore(pool, _ctx(role=Role.VIEWER), resolver, sid, "cp")
        finally:
            await pool.close()

    asyncio.run(scenario())

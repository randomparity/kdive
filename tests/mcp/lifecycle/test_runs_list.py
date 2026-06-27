"""runs.list handler tests — the Run catalog view (#623, ADR-0198).

The handler is driven directly with an injected pool. Seeding inserts a Resource,
Allocation, System, and Investigation, then a Run, so each filter axis (system_id /
investigation_id / state), the no-leak scoping, and keyset pagination are exercised
independently. Mirrors tests/mcp/lifecycle/test_systems_list.py.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.runs.list import RunsListRequest, list_runs
from kdive.security.authz.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config": {"kind": "local", "path": "/configs/kdump.config"},
}

_PROV_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
            "crashkernel": "256M",
        }
    },
}


def _ctx(
    *,
    projects: tuple[str, ...] = ("proj",),
    roles: dict[str, Role] | None = None,
) -> RequestContext:
    if roles is None:
        roles = {p: Role.VIEWER for p in projects}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


async def _list_runs(
    pool: AsyncConnectionPool, ctx: RequestContext, **filters: Any
) -> ToolResponse:
    return await list_runs(pool, ctx, RunsListRequest(**filters))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_resource(pool: AsyncConnectionPool) -> UUID:
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
    return res.id


async def _seed_allocation(pool: AsyncConnectionPool, *, project: str, resource_id: UUID) -> UUID:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=resource_id,
                state=AllocationState.GRANTED,
            ),
        )
    return alloc.id


async def _seed_system(pool: AsyncConnectionPool, *, project: str, allocation_id: UUID) -> UUID:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=allocation_id,
                state=SystemState.READY,
                provisioning_profile=copy.deepcopy(_PROV_PROFILE),
            ),
        )
    return system.id


async def _seed_investigation(pool: AsyncConnectionPool, *, project: str) -> UUID:
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
                state=InvestigationState.OPEN,
            ),
        )
    return inv.id


async def _seed_run(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    state: RunState = RunState.RUNNING,
    system_id: UUID | None = None,
    investigation_id: UUID | None = None,
    failure: ErrorCategory | None = None,
    created_at: datetime = _DT,
    label: str | None = None,
) -> UUID:
    """Insert a Run, seeding its prerequisite Investigation/System when not supplied."""
    if investigation_id is None:
        investigation_id = await _seed_investigation(pool, project=project)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=created_at,
                updated_at=created_at,
                principal="user-1",
                project=project,
                investigation_id=investigation_id,
                system_id=system_id,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile=copy.deepcopy(_PROFILE),
                failure_category=failure,
                label=label,
            ),
        )
    return run.id


async def _seed_bound_system(pool: AsyncConnectionPool, *, project: str = "proj") -> UUID:
    res = await _seed_resource(pool)
    alloc = await _seed_allocation(pool, project=project, resource_id=res)
    return await _seed_system(pool, project=project, allocation_id=alloc)


def test_lists_callers_runs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_run(pool, state=RunState.RUNNING)
            await _seed_run(pool, state=RunState.CREATED)
            resp = await _list_runs(pool, _ctx())
        assert resp.object_id == "runs"
        assert resp.status == "ok"
        assert len(resp.items) == 2

    asyncio.run(_run())


def test_item_carries_investigation_and_target_kind(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv = await _seed_investigation(pool, project="proj")
            await _seed_run(pool, investigation_id=inv, state=RunState.RUNNING)
            resp = await _list_runs(pool, _ctx())
        assert resp.status == "ok" and len(resp.items) == 1
        item = resp.items[0]
        assert item.data["investigation_id"] == str(inv)
        assert item.data["target_kind"] == "local-libvirt"

    asyncio.run(_run())


def test_item_carries_label(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv = await _seed_investigation(pool, project="proj")
            await _seed_run(pool, investigation_id=inv, state=RunState.RUNNING, label="repro-A")
            await _seed_run(pool, investigation_id=inv, state=RunState.CREATED)
            resp = await _list_runs(pool, _ctx())
        labels = {item.data["label"] for item in resp.items}
        assert labels == {None, "repro-A"}

    asyncio.run(_run())


def test_filter_by_system_id_excludes_unbound(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_bound_system(pool)
            on_system = await _seed_run(pool, system_id=system_id, state=RunState.RUNNING)
            await _seed_run(pool, system_id=None, state=RunState.RUNNING)  # unbound, excluded
            other_system = await _seed_bound_system(pool)
            await _seed_run(pool, system_id=other_system, state=RunState.RUNNING)
            resp = await _list_runs(pool, _ctx(), system_id=str(system_id))
        assert [r.object_id for r in resp.items] == [str(on_system)]

    asyncio.run(_run())


def test_filter_by_investigation_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_a = await _seed_investigation(pool, project="proj")
            inv_b = await _seed_investigation(pool, project="proj")
            mine = await _seed_run(pool, investigation_id=inv_a, state=RunState.RUNNING)
            await _seed_run(pool, investigation_id=inv_b, state=RunState.RUNNING)
            resp = await _list_runs(pool, _ctx(), investigation_id=str(inv_a))
        assert [r.object_id for r in resp.items] == [str(mine)]

    asyncio.run(_run())


def test_filter_by_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            running = await _seed_run(pool, state=RunState.RUNNING)
            await _seed_run(pool, state=RunState.CREATED)
            resp = await _list_runs(pool, _ctx(), state="running")
        assert [r.object_id for r in resp.items] == [str(running)]

    asyncio.run(_run())


def test_unknown_state_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_runs(pool, _ctx(), state="bogus")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_state"
        assert "running" in cast(list[str], resp.data["accepted_values"])

    asyncio.run(_run())


@pytest.mark.parametrize("bad_state", ["", "torn", "RUNNING"])
def test_state_filter_rejects_invalid_values(migrated_url: str, bad_state: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_runs(pool, _ctx(), state=bad_state)
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_malformed_system_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_runs(pool, _ctx(), system_id="not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "not-a-uuid" in resp.detail

    asyncio.run(_run())


def test_malformed_investigation_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_runs(pool, _ctx(), investigation_id="nope")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"

    asyncio.run(_run())


def test_ungranted_project_run_is_omitted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            mine = await _seed_run(pool, project="proj", state=RunState.RUNNING)
            await _seed_run(pool, project="other", state=RunState.RUNNING)
            resp = await _list_runs(pool, _ctx(projects=("proj",)))
        assert [r.object_id for r in resp.items] == [str(mine)]

    asyncio.run(_run())


def test_member_without_role_run_is_omitted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_run(pool, project="proj", state=RunState.RUNNING)
            ctx = RequestContext(
                principal="user-1", agent_session="s", projects=("proj",), roles={}
            )
            resp = await _list_runs(pool, ctx)
        assert resp.items == []

    asyncio.run(_run())


def test_no_viewer_projects_returns_empty_collection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = RequestContext(principal="user-1", agent_session="s", projects=(), roles={})
            resp = await _list_runs(pool, ctx)
        assert resp.status == "ok"
        assert resp.items == []

    asyncio.run(_run())


def test_validation_precedes_scoping(migrated_url: str) -> None:
    """A malformed filter is a configuration_error even with no viewer projects (ADR-0198)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = RequestContext(principal="user-1", agent_session="s", projects=(), roles={})
            bad_state = await _list_runs(pool, ctx, state="bogus")
            bad_cursor = await _list_runs(pool, ctx, cursor="!!!")
        assert bad_state.error_category == "configuration_error"
        assert bad_state.data["reason"] == "invalid_state"
        assert bad_cursor.error_category == "configuration_error"
        assert bad_cursor.data["reason"] == "invalid_cursor"

    asyncio.run(_run())


def test_failed_run_renders_failure_item(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_run(pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE)
            resp = await _list_runs(pool, _ctx())
        assert resp.status == "ok" and len(resp.items) == 1
        item = resp.items[0]
        assert item.status == "error"
        assert item.error_category == "build_failure"

    asyncio.run(_run())


def test_cap_applies_after_filters_no_undercount(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            target = await _seed_bound_system(pool)
            other = await _seed_bound_system(pool)
            # The matching Run is the OLDEST (would fall off a cap-then-filter page).
            matched = await _seed_run(
                pool,
                system_id=target,
                state=RunState.RUNNING,
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
            for i in range(5):
                await _seed_run(
                    pool,
                    system_id=other,
                    state=RunState.RUNNING,
                    created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
                )
            resp = await _list_runs(pool, _ctx(), system_id=str(target), limit=3)
        assert [r.object_id for r in resp.items] == [str(matched)]

    asyncio.run(_run())


def test_limit_clamps_to_range(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _seed_run(
                    pool,
                    state=RunState.RUNNING,
                    created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
                )
            resp_lo = await _list_runs(pool, _ctx(), limit=0)
            resp_hi = await _list_runs(pool, _ctx(), limit=10_000)
        assert len(resp_lo.items) == 1  # min clamp to 1
        assert len(resp_hi.items) == 3  # max clamp does not error; returns all

    asyncio.run(_run())


def test_pagination_drains_distinct_timestamps(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(5):
                await _seed_run(
                    pool,
                    state=RunState.RUNNING,
                    created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
                )
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):
                page = await _list_runs(pool, _ctx(), limit=2, cursor=cursor)
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                cursor = cast(str, page.data["next_cursor"])
        assert len(seen) == 5
        assert len(set(seen)) == 5

    asyncio.run(_run())


def test_pagination_drains_tied_timestamps(migrated_url: str) -> None:
    async def _run() -> None:
        tie = datetime(2026, 6, 1, tzinfo=UTC)
        async with _pool(migrated_url) as pool:
            for _ in range(5):
                await _seed_run(pool, state=RunState.RUNNING, created_at=tie)
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):
                page = await _list_runs(pool, _ctx(), limit=2, cursor=cursor)
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                cursor = cast(str, page.data["next_cursor"])
        assert len(seen) == 5
        assert len(set(seen)) == 5

    asyncio.run(_run())


def test_pagination_no_truncation_at_exactly_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(2):
                await _seed_run(
                    pool,
                    state=RunState.RUNNING,
                    created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
                )
            resp = await _list_runs(pool, _ctx(), limit=2)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None

    asyncio.run(_run())


def test_pagination_malformed_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_run(pool, state=RunState.RUNNING)
            resp = await _list_runs(pool, _ctx(), limit=2, cursor="!!!")
        assert resp.status == "error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())


def test_cursor_from_another_tool_is_rejected(migrated_url: str) -> None:
    """A cursor minted by a different list tool is invalid_cursor, not a silent first page."""

    async def _run() -> None:
        from kdive.mcp.tools._common import encode_ts_uuid_cursor

        async with _pool(migrated_url) as pool:
            await _seed_run(pool, state=RunState.RUNNING)
            foreign = encode_ts_uuid_cursor("systems.list", _DT, uuid4())
            resp = await _list_runs(pool, _ctx(), cursor=foreign)
        assert resp.status == "error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())

"""debug.get_session / debug.list_sessions read tools (ADR-0176, #571).

Handlers are called directly with an injected pool — no transport, no Connector. The
seeding mirrors test_debug_tools.py: a granted Allocation, a ready System, a succeeded
Run, and debug_sessions rows in the requested state. These cover recovery (start, lose
local state, recover via list/get, then end), the filters, and the no-leak boundary.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Allocation, DebugSession, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.debug.sessions import read as sessions_read
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.mcp.tools.lifecycle.systems.view import get_system as _get_system
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.ports.lifecycle import (
    TransportHandleData,
    TransportHandleKind,
)
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.debug.sessions import active_session_ids_for_run, active_session_ids_for_system
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery  # isort: skip

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
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
        }
    },
}


def _ctx(
    role: Role | None = Role.VIEWER,
    *,
    projects: tuple[str, ...] = ("proj",),
    roles: dict[str, Role] | None = None,
) -> RequestContext:
    resolved = roles if roles is not None else ({"proj": role} if role is not None else {})
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=resolved)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _granted_allocation(pool: AsyncConnectionPool, *, project: str = "proj") -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=2
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
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
                state=AllocationState.GRANTED,
            ),
        )
    return str(alloc.id)


async def _seed_system(pool: AsyncConnectionPool, alloc_id: str, *, project: str = "proj") -> str:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=UUID(alloc_id),
                state=SystemState.READY,
                provisioning_profile=copy.deepcopy(_PROFILE),
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


async def _seed_run(pool: AsyncConnectionPool, sys_id: str, *, project: str = "proj") -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
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
                project=project,
                investigation_id=inv.id,
                system_id=UUID(sys_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
    return str(run.id)


async def _seed_session(
    pool: AsyncConnectionPool,
    run_id: str,
    state: DebugSessionState,
    *,
    transport: str = "gdbstub",
    project: str = "proj",
) -> str:
    port = 22 if transport == "drgn-live" else 1234
    handle_kind = cast(TransportHandleKind, transport)
    async with pool.connection() as conn:
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                run_id=UUID(run_id),
                state=state,
                transport=transport,
                transport_handle=TransportHandleData(
                    kind=handle_kind, host="127.0.0.1", port=port
                ).encode(),
            ),
        )
    return str(session.id)


async def _seeded_session(
    pool: AsyncConnectionPool, state: DebugSessionState
) -> tuple[str, str, str]:
    alloc_id = await _granted_allocation(pool)
    sys_id = await _seed_system(pool, alloc_id)
    run_id = await _seed_run(pool, sys_id)
    session_id = await _seed_session(pool, run_id, state)
    return session_id, run_id, sys_id


# --- debug.get_session ---------------------------------------------------------------------


def test_get_session_returns_visible_session(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, run_id, sys_id = await _seeded_session(pool, DebugSessionState.LIVE)
            resp = await sessions_read.get_session(pool, _ctx(), session_id)
        assert resp.status == "live"
        assert resp.object_id == session_id
        assert resp.data["run_id"] == run_id
        assert resp.data["system_id"] == sys_id
        assert resp.data["transport"] == "gdbstub"
        assert resp.data["project"] == "proj"
        assert "debug.end_session" in resp.suggested_next_actions

    asyncio.run(_run())


def test_get_session_detached_offers_only_reread(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, _, _ = await _seeded_session(pool, DebugSessionState.DETACHED)
            resp = await sessions_read.get_session(pool, _ctx(), session_id)
        assert resp.status == "detached"
        assert "debug.end_session" not in resp.suggested_next_actions

    asyncio.run(_run())


def test_get_session_malformed_id_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await sessions_read.get_session(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "session_id" in resp.detail

    asyncio.run(_run())


def test_get_session_absent_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await sessions_read.get_session(pool, _ctx(), str(uuid4()))
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_get_session_cross_project_is_not_found_indistinguishable(migrated_url: str) -> None:
    # A session in a project the caller cannot view must read byte-identically to an absent
    # one: no cross-project existence leak (ADR-0097/ADR-0123).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, _, _ = await _seeded_session(pool, DebugSessionState.LIVE)
            cross = await sessions_read.get_session(pool, _ctx(projects=("other",)), session_id)
            absent = await sessions_read.get_session(pool, _ctx(projects=("other",)), str(uuid4()))
        assert cross.status == "error" and cross.error_category == "not_found"
        assert cross.model_dump(exclude={"object_id"}) == absent.model_dump(exclude={"object_id"})

    asyncio.run(_run())


# --- debug.list_sessions -------------------------------------------------------------------


def test_list_sessions_returns_only_callers_sessions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            mine, _, _ = await _seeded_session(pool, DebugSessionState.LIVE)
            other_alloc = await _granted_allocation(pool, project="other")
            other_sys = await _seed_system(pool, other_alloc, project="other")
            other_run = await _seed_run(pool, other_sys, project="other")
            await _seed_session(pool, other_run, DebugSessionState.LIVE, project="other")
            resp = await sessions_read.list_sessions(pool, _ctx())
        ids = {item.object_id for item in resp.items}
        assert resp.data["count"] == 1
        assert ids == {mine}

    asyncio.run(_run())


def test_list_sessions_empty_membership_is_empty_collection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seeded_session(pool, DebugSessionState.LIVE)
            resp = await sessions_read.list_sessions(pool, _ctx(role=None))
        assert resp.status == "ok" and resp.data["count"] == 0

    asyncio.run(_run())


def test_list_sessions_filters_by_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc)
            run_a = await _seed_run(pool, sys_id)
            run_b = await _seed_run(pool, sys_id)
            a_session = await _seed_session(pool, run_a, DebugSessionState.LIVE)
            await _seed_session(pool, run_b, DebugSessionState.LIVE, transport="drgn-live")
            resp = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(run_id=run_a)
            )
        assert {item.object_id for item in resp.items} == {a_session}

    asyncio.run(_run())


def test_list_sessions_filters_by_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, _, sys_id = await _seeded_session(pool, DebugSessionState.LIVE)
            # A second System with its own session must be excluded by the system filter.
            other_alloc = await _granted_allocation(pool)
            other_sys = await _seed_system(pool, other_alloc)
            other_run = await _seed_run(pool, other_sys)
            await _seed_session(pool, other_run, DebugSessionState.LIVE)
            resp = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(system_id=sys_id)
            )
        assert {item.object_id for item in resp.items} == {session_id}

    asyncio.run(_run())


def test_list_sessions_filters_by_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc)
            run_a = await _seed_run(pool, sys_id)
            run_b = await _seed_run(pool, sys_id)
            live = await _seed_session(pool, run_a, DebugSessionState.LIVE)
            await _seed_session(pool, run_b, DebugSessionState.DETACHED)
            resp = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(state="live")
            )
        assert {item.object_id for item in resp.items} == {live}

    asyncio.run(_run())


def test_list_sessions_returns_cursor_for_next_page(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc)
            expected: set[str] = set()
            for _ in range(3):
                run_id = await _seed_run(pool, sys_id)
                expected.add(await _seed_session(pool, run_id, DebugSessionState.LIVE))

            first = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(limit=2)
            )
            next_cursor = first.data["next_cursor"]
            assert first.data["truncated"] is True
            assert isinstance(next_cursor, str)
            assert len(first.items) == 2

            second = await sessions_read.list_sessions(
                pool,
                _ctx(),
                sessions_read.SessionsListRequest(limit=2, cursor=next_cursor),
            )
        assert second.data["truncated"] is False
        assert second.data["next_cursor"] is None
        assert {item.object_id for item in first.items}.isdisjoint(
            {item.object_id for item in second.items}
        )
        assert {item.object_id for item in first.items + second.items} == expected

    asyncio.run(_run())


def test_list_sessions_cross_project_filter_yields_nothing(migrated_url: str) -> None:
    # A `project` filter naming a non-member project is intersected with membership, so it
    # returns zero rows rather than leaking that the project has sessions.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            other_alloc = await _granted_allocation(pool, project="other")
            other_sys = await _seed_system(pool, other_alloc, project="other")
            other_run = await _seed_run(pool, other_sys, project="other")
            await _seed_session(pool, other_run, DebugSessionState.LIVE, project="other")
            resp = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(project="other")
            )
        assert resp.data["count"] == 0

    asyncio.run(_run())


def test_list_sessions_bad_filter_uuid_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            bad_run = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(run_id="nope")
            )
            bad_system = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(system_id="nope")
            )
        assert bad_run.status == "error" and bad_run.error_category == "configuration_error"
        assert bad_run.data["reason"] == "invalid_uuid"
        assert bad_run.detail is not None and "run_id" in bad_run.detail
        assert bad_system.status == "error"
        assert bad_system.error_category == "configuration_error"
        assert bad_system.data["reason"] == "invalid_uuid"
        assert bad_system.detail is not None and "system_id" in bad_system.detail

    asyncio.run(_run())


def test_list_sessions_bad_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(cursor="not-a-token")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())


def test_list_sessions_bad_state_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await sessions_read.list_sessions(
                pool, _ctx(), sessions_read.SessionsListRequest(state="bogus")
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- recovery flow + run/system surfacing --------------------------------------------------


def test_recovery_flow_list_get_then_active_ids(migrated_url: str) -> None:
    # Start a session, lose the local handle, recover it via list_sessions/get_session, and
    # confirm the active id is surfaced via the read helpers the run/system gets use.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, run_id, sys_id = await _seeded_session(pool, DebugSessionState.LIVE)
            listed = await sessions_read.list_sessions(pool, _ctx())
            recovered = listed.items[0].object_id
            assert recovered == session_id
            got = await sessions_read.get_session(pool, _ctx(), recovered)
            assert got.status == "live"
            async with pool.connection() as conn:
                by_run = await active_session_ids_for_run(conn, UUID(run_id))
                by_system = await active_session_ids_for_system(conn, UUID(sys_id))
        assert by_run == [session_id]
        assert by_system == [session_id]

    asyncio.run(_run())


def test_active_session_ids_exclude_detached(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, run_id, sys_id = await _seeded_session(pool, DebugSessionState.DETACHED)
            async with pool.connection() as conn:
                by_run = await active_session_ids_for_run(conn, UUID(run_id))
                by_system = await active_session_ids_for_system(conn, UUID(sys_id))
        assert by_run == []
        assert by_system == []

    asyncio.run(_run())


def test_runs_get_surfaces_active_debug_session_ids(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, run_id, _ = await _seeded_session(pool, DebugSessionState.LIVE)
            resp = await _get_run(
                pool, _ctx(), run_id, resolver=provider_resolver(), secret_registry=SecretRegistry()
            )
        assert resp.data["active_debug_session_ids"] == [session_id]

    asyncio.run(_run())


def test_systems_get_surfaces_active_debug_session_ids(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id, _, sys_id = await _seeded_session(pool, DebugSessionState.LIVE)
            resp = await _get_system(pool, _ctx(), sys_id, resolver=provider_resolver())
        assert resp.data["active_debug_session_ids"] == [session_id]

    asyncio.run(_run())


def test_runs_get_active_debug_session_ids_empty_when_detached(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, run_id, _ = await _seeded_session(pool, DebugSessionState.DETACHED)
            resp = await _get_run(
                pool, _ctx(), run_id, resolver=provider_resolver(), secret_registry=SecretRegistry()
            )
        assert resp.data["active_debug_session_ids"] == []

    asyncio.run(_run())

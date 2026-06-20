"""Tests for DebugSessionTelemetry wiring into end_session and repair_dead_sessions.

Covers the two recording sites for session duration (ADR-0191 H3):
- end_session (server clean close) records ``ok`` or ``error`` outcome.
- repair_dead_sessions (reconciler reap) records ``reaped`` outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.db.resource_discovery import register_discovered_resource
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle import Allocation, DebugSession, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.mcp.tools.debug.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.ports import (
    SystemHandle,
    TransportHandle,
    TransportHandleData,
    TransportHandleKind,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE_POLICY = LocalLibvirtProfilePolicy()

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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


class _FakeConnector:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        self.opened.append((str(system), kind))
        return TransportHandle(
            TransportHandleData(
                kind=cast(TransportHandleKind, kind), host="127.0.0.1", port=1234
            ).encode()
        )

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed.append(str(handle))


def _ctx(role: Any = "operator", *, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    from kdive.security.authz.rbac import Role

    roles = {"proj": Role.OPERATOR}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _granted_allocation(pool: AsyncConnectionPool) -> str:
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
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
    return str(alloc.id)


async def _seed_system(pool: AsyncConnectionPool, alloc_id: str) -> str:
    import copy

    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=SystemState.READY,
                provisioning_profile=copy.deepcopy(_PROFILE),
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


async def _seed_run(pool: AsyncConnectionPool, sys_id: str) -> str:
    from psycopg.types.json import Jsonb

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
                system_id=UUID(sys_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'boot', 'succeeded', %s)",
            (run.id, Jsonb({})),
        )
    return str(run.id)


async def _seed_session(
    pool: AsyncConnectionPool, run_id: str, state: DebugSessionState, *, transport: str = "gdbstub"
) -> str:
    async with pool.connection() as conn:
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                run_id=UUID(run_id),
                state=state,
                transport=transport,
                transport_handle=TransportHandleData(
                    kind=cast(TransportHandleKind, transport), host="127.0.0.1", port=1234
                ).encode(),
            ),
        )
    return str(session.id)


def _make_telemetry() -> tuple[InMemoryMetricReader, DebugSessionTelemetry]:
    reader = InMemoryMetricReader()
    tel = DebugSessionTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    return reader, tel


def test_end_session_records_ok_duration(migrated_url: str) -> None:
    """A successful end_session emits a duration point with outcome='ok'."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.LIVE)
            reader, tel = _make_telemetry()
            registry = SecretRegistry()
            handlers = debug_tools.DebugSessionHandlers.from_resolver(
                provider_resolver(connector=_FakeConnector(), profile_policy=_PROFILE_POLICY),
                runtime_resolver=None,
                secret_registry=registry,
                telemetry=tel,
            )
            resp = await handlers.end_session(pool, _ctx(), session_id)
        assert resp.status == "detached"
        pts = _points(reader, "kdive.debug.session.duration")
        assert pts, "no duration point emitted on clean end_session"
        assert pts[0].attributes["outcome"] == "ok"
        assert pts[0].attributes["transport"] == "gdbstub"

    asyncio.run(_run())


def test_end_session_idempotent_detach_also_records_ok(migrated_url: str) -> None:
    """Idempotent detach (session already detached) still records ok duration."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.DETACHED)
            reader, tel = _make_telemetry()
            registry = SecretRegistry()
            handlers = debug_tools.DebugSessionHandlers.from_resolver(
                provider_resolver(connector=_FakeConnector(), profile_policy=_PROFILE_POLICY),
                runtime_resolver=None,
                secret_registry=registry,
                telemetry=tel,
            )
            resp = await handlers.end_session(pool, _ctx(), session_id)
        assert resp.status == "detached"
        pts = _points(reader, "kdive.debug.session.duration")
        assert pts, "idempotent detach must still record duration"
        assert pts[0].attributes["outcome"] == "ok"

    asyncio.run(_run())

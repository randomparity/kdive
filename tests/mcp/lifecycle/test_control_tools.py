"""control.* tool + handler tests — handlers called directly with injected pool + control."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    DEBUG_SESSIONS,
    INVESTIGATIONS,
    RUNS,
    SYSTEMS,
)
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, DebugSession, Investigation, Run, System
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs import queue
from kdive.jobs.handlers.control import control as control_plane
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import WATCH_MAX_DEADLINE_S, PowerPayload, SystemPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.control import registrar as control_tools
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _profile(*, destructive_ops: list[str] | None = None) -> dict[str, Any]:
    data = copy.deepcopy(_PROFILE)
    if destructive_ops is not None:
        data["provider"]["local-libvirt"]["destructive_ops"] = destructive_ops
    return data


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


def _admin_ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.ADMIN}
    )


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


async def _seed_system(
    pool: AsyncConnectionPool,
    alloc_id: str,
    state: SystemState,
    *,
    destructive_ops: list[str] | None = None,
    domain_name: str | None = None,
) -> str:
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
                state=state,
                provisioning_profile=_profile(destructive_ops=destructive_ops),
                domain_name=domain_name,
            ),
        )
    return str(system.id)


async def _seed_live_session(pool: AsyncConnectionPool, sys_id: str) -> str:
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
                state=RunState.RUNNING,
                build_profile={},
            ),
        )
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                run_id=run.id,
                state=DebugSessionState.LIVE,
                transport="gdbstub",
            ),
        )
    return str(session.id)


class _FakeControl:
    """Records power/force_crash calls; never raises."""

    def __init__(self) -> None:
        self.powered: list[tuple[str, str]] = []
        self.crashed: list[str] = []

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.powered.append((domain_name, action.value))

    def force_crash(self, domain_name: str) -> None:
        self.crashed.append(domain_name)


async def _power(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    action: str,
    idempotency_key: str | None = None,
) -> Any:
    return await control_tools.power_system(
        pool,
        ctx,
        system_id=system_id,
        action=action,
        idempotency_key=idempotency_key,
    )


# --- control.power tool --------------------------------------------------------------------


def test_power_off_enqueues_job(migrated_url: str) -> None:
    # power off/cycle/reset are contributor leaseholder lifecycle — no opt-in (ADR-0320).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action="off")
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'power' AND dedup_key LIKE %s",
                    (f"{sys_id}:power:off:%",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_power_keyed_retry_replays_one_job(migrated_url: str) -> None:
    """A repeated key folds into the dedup key: identical envelope, exactly one power job."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            first = await _power(pool, _ctx(), system_id=sys_id, action="on", idempotency_key="k1")
            second = await _power(pool, _ctx(), system_id=sys_id, action="on", idempotency_key="k1")
            assert first.model_dump() == second.model_dump()
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'power' AND dedup_key = %s",
                    (f"{sys_id}:power:on:k1",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_power_unkeyed_calls_are_distinct_jobs(migrated_url: str) -> None:
    """Without a key, each power call is a distinct job (the default, ADR-0193)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            await _power(pool, _ctx(), system_id=sys_id, action="on")
            await _power(pool, _ctx(), system_id=sys_id, action="on")
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'power' AND dedup_key LIKE %s",
                    (f"{sys_id}:power:on:%",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 2

    asyncio.run(_run())


def test_power_on_is_contributor_and_enqueues_job(migrated_url: str) -> None:
    # power on brings a READY System up — contributor leaseholder lifecycle (ADR-0320).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action="on")
        assert resp.status == "queued"

    asyncio.run(_run())


@pytest.mark.parametrize("action", ["off", "cycle", "reset"])
def test_power_destructive_action_allowed_for_contributor_no_optin(
    migrated_url: str, action: str
) -> None:
    # off/cycle/reset need no destructive_ops opt-in and no admin: contributor suffices (ADR-0320).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action=action)
            assert resp.status == "queued"

    asyncio.run(_run())


def test_power_unknown_action_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(), system_id=sys_id, action="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_power_non_ready_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.DEFINED)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action="off")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "defined"

    asyncio.run(_run())


def test_resume_admitted_only_from_paused(migrated_url: str) -> None:
    # #1254: resume is the one action admitted from PAUSED (a start_paused restore's guest).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PAUSED)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action="resume")
        assert resp.status == "queued"

    asyncio.run(_run())


def test_resume_refused_from_ready(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action="resume")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "ready"

    asyncio.run(_run())


def test_non_resume_action_refused_from_paused(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PAUSED)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action="off")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "paused"

    asyncio.run(_run())


@pytest.mark.parametrize("action", ["on", "off", "cycle", "reset"])
def test_power_on_crashed_system_is_config_error(migrated_url: str, action: str) -> None:
    # A CRASHED System holds crash evidence: power is refused, protecting it (ADR-0320).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action=action)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "crashed"

    asyncio.run(_run())


@pytest.mark.parametrize("action", ["on", "off", "cycle", "reset"])
def test_power_on_crashing_system_is_config_error(migrated_url: str, action: str) -> None:
    # A CRASHING System is mid-force_crash; power is refused, protecting crash evidence (#1078).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHING)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action=action)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "crashing"

    asyncio.run(_run())


def test_power_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(projects=("other",)), system_id=sys_id, action="off")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_power_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _power(pool, _ctx(), system_id="not-a-uuid", action="off")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


@pytest.mark.parametrize("action", ["on", "off", "cycle", "reset"])
def test_power_denied_for_viewer(migrated_url: str, action: str) -> None:
    # contributor is the floor for every power action; a viewer is refused (ADR-0320).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await _power(pool, _ctx(Role.VIEWER), system_id=sys_id, action=action)

    asyncio.run(_run())


def test_power_handler_calls_provider_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, domain_name="kdive-x")
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.POWER,
                    PowerPayload(system_id=sys_id, action=PowerAction.RESET),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{sys_id}:power:reset:{uuid4()}",
                )
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_plane.power_handler(
                    conn, job, resolver=provider_resolver(controller=ctrl)
                )
            assert ctrl.powered == [("kdive-x", "reset")]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_id = %s AND transition = 'power:reset'",
                    (sys_id,),
                )
                audit_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "ready"  # no state move
        assert audit_row is not None and audit_row["n"] == 1

    asyncio.run(_run())


def test_power_handler_refuses_non_ready_system(migrated_url: str) -> None:
    # A power job admitted READY but executed after ready->crashing/crashed must fail terminally
    # and never drive the physical domain — protecting crash evidence at execution (ADR-0320).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED, domain_name="kdive-x")
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.POWER,
                    PowerPayload(system_id=sys_id, action=PowerAction.RESET),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{sys_id}:power:reset:{uuid4()}",
                )
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await control_plane.power_handler(
                        conn, job, resolver=provider_resolver(controller=ctrl)
                    )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.terminal is True  # dead-letters, does not retry (ADR-0320)
            assert ctrl.powered == []  # physical power op never invoked

    asyncio.run(_run())


def test_power_handler_missing_system_is_infra_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.POWER,
                    PowerPayload(system_id=str(uuid4()), action=PowerAction.OFF),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{uuid4()}:power:off:{uuid4()}",
                )
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await control_plane.power_handler(
                        conn, job, resolver=provider_resolver(controller=ctrl)
                    )
        assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())


# --- control.force_crash tool (gate + admission) -------------------------------------------


async def _crash(pool: AsyncConnectionPool, ctx: RequestContext, sys_id: str) -> Any:
    return await control_tools.force_crash_system(
        pool, ctx, system_id=sys_id, resolver=provider_resolver()
    )


def _operator_ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
    )


@pytest.mark.parametrize(
    ("is_admin", "opt_in", "expected_missing"),
    [
        (False, True, "admin_role"),
        (True, False, "profile_opt_in"),
    ],
)
def test_force_crash_denied_returns_authorization_denied(
    migrated_url: str, is_admin: bool, opt_in: bool, expected_missing: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ops = ["force_crash"] if opt_in else []
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, destructive_ops=ops)
            ctx = _admin_ctx() if is_admin else _operator_ctx()
            resp = await _crash(pool, ctx, sys_id)
            assert resp.status == "error" and resp.error_category == "authorization_denied"
            assert resp.data["missing_checks"] == [expected_missing]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_id = %s AND transition = 'force_crash:denied'",
                    (sys_id,),
                )
                audit_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'force_crash'")
                jobs_row = await cur.fetchone()
        assert audit_row is not None and audit_row["n"] == 1
        assert jobs_row is not None and jobs_row["n"] == 0  # no job enqueued on denial

    asyncio.run(_run())


def test_force_crash_allowed_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(
                pool, alloc_id, SystemState.READY, destructive_ops=["force_crash"]
            )
            resp = await _crash(pool, _admin_ctx(), sys_id)
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{sys_id}:force_crash",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_force_crash_non_ready_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(
                pool, alloc_id, SystemState.CRASHED, destructive_ops=["force_crash"]
            )
            resp = await _crash(pool, _admin_ctx(), sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "crashed"

    asyncio.run(_run())


def test_force_crash_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _crash(pool, _admin_ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- control.force_crash handler -----------------------------------------------------------


async def _enqueue_crash(pool: AsyncConnectionPool, sys_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.FORCE_CRASH,
            SystemPayload(system_id=sys_id),
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{sys_id}:force_crash",
        )


def test_force_crash_handler_crashes_and_detaches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, domain_name="kdive-x")
            session_id = await _seed_live_session(pool, sys_id)
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(
                    conn, job, resolver=provider_resolver(controller=ctrl)
                )
            assert ctrl.crashed == ["kdive-x"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
                sess_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_kind = 'debug_sessions' AND transition = 'live->detached'"
                )
                detach_audit = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "crashed"
        assert sess_row is not None and sess_row["state"] == "detached"
        assert detach_audit is not None and detach_audit["n"] == 1

    asyncio.run(_run())


def test_force_crash_handler_no_session_is_noop_detach(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, domain_name="kdive-x")
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(
                    conn, job, resolver=provider_resolver(controller=ctrl)
                )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "crashed"

    asyncio.run(_run())


def test_force_crash_handler_already_crashed_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED, domain_name="kdive-x")
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(
                    conn, job, resolver=provider_resolver(controller=ctrl)
                )  # no raise
            assert ctrl.crashed == []  # already CRASHED: force_crash is a no-op, NMI not re-fired
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'crashing->crashed'"
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0  # no transition audited on idempotent re-run

    asyncio.run(_run())


def test_force_crash_handler_terminal_system_does_not_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(
                pool, alloc_id, SystemState.TORN_DOWN, domain_name="kdive-x"
            )
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(
                    conn, job, resolver=provider_resolver(controller=ctrl)
                )
            assert ctrl.crashed == []  # teardown won the race; no NMI

    asyncio.run(_run())


def test_force_crash_dedup_key_is_canonical_for_non_canonical_uuid(migrated_url: str) -> None:
    # The reconciler's leak-recovery predicate matches the dedup_key against `s.id::text`
    # (canonical). Admission must mint the key from the canonical UUID even when the agent passes
    # a non-canonical form (uppercase), or the reconciler misses the live job (#1078).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(
                pool, alloc_id, SystemState.READY, destructive_ops=["force_crash"]
            )
            resp = await _crash(pool, _admin_ctx(), sys_id.upper())  # non-canonical input
            assert resp.status != "error", resp
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT dedup_key FROM jobs WHERE kind = 'force_crash' "
                    "AND payload->>'system_id' = %s",
                    (sys_id.upper(),),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["dedup_key"] == f"{sys_id}:force_crash"  # canonical lowercase

    asyncio.run(_run())


def test_force_crash_handler_missing_system_is_infra_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job = await _enqueue_crash(pool, str(uuid4()))
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await control_plane.force_crash_handler(
                        conn, job, resolver=provider_resolver(controller=ctrl)
                    )
        assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())


# --- registration --------------------------------------------------------------------------


def test_register_handlers_binds_power_and_force_crash() -> None:
    registry = HandlerRegistry()
    control_plane.register_handlers(registry, resolver=provider_resolver(controller=_FakeControl()))
    assert registry.get(JobKind.POWER) is not None
    assert registry.get(JobKind.FORCE_CRASH) is not None


async def _diagnostic_sysrq(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    command: str,
    resolver: ProviderResolver | None = None,
) -> Any:
    return await control_tools.diagnostic_sysrq_system(
        pool,
        ctx,
        system_id=system_id,
        command=command,
        resolver=resolver if resolver is not None else provider_resolver(),
        idempotency_key=None,
    )


def test_diagnostic_sysrq_enqueues_job_for_contributor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _diagnostic_sysrq(
                pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, command="show_blocked_tasks"
            )
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT payload FROM jobs WHERE kind = 'diagnostic_sysrq' "
                    "AND dedup_key LIKE %s",
                    (f"{sys_id}:diagnostic_sysrq:show_blocked_tasks:%",),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["payload"]["command"] == "show_blocked_tasks"

    asyncio.run(_run())


def test_diagnostic_sysrq_unknown_command_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _diagnostic_sysrq(
                pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, command="show_everything"
            )
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "unknown_command"

    asyncio.run(_run())


def test_diagnostic_sysrq_destructive_command_redirects_to_force_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _diagnostic_sysrq(
                pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, command="crash"
            )
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "destructive_command"
            assert "control.force_crash" in str(resp.data["remediation"])

    asyncio.run(_run())


def test_diagnostic_sysrq_not_ready_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            resp = await _diagnostic_sysrq(
                pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, command="show_memory"
            )
            assert resp.error_category == "configuration_error"
            assert resp.data["current_status"] == "provisioning"

    asyncio.run(_run())


def test_diagnostic_sysrq_viewer_is_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await _diagnostic_sysrq(
                    pool, _ctx(Role.VIEWER), system_id=sys_id, command="show_memory"
                )

    asyncio.run(_run())


def test_diagnostic_sysrq_non_local_provider_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            disc = LocalLibvirtDiscovery(
                host_uri="qemu:///system",
                connect=lambda: FakeLibvirtConn(),
                concurrent_allocation_cap=2,
            )
            async with pool.connection() as conn:
                res = await register_discovered_resource(
                    conn, disc.list_resources()[0], pool="remote-libvirt", cost_class="local"
                )
                await conn.execute(
                    "UPDATE resources SET kind = 'remote-libvirt' WHERE id = %s", (res.id,)
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
                        state=SystemState.READY,
                        provisioning_profile=_PROFILE,
                    ),
                )
            runtime = provider_resolver().runtimes()[0]
            resolver = ProviderResolver(
                {
                    ResourceKind.LOCAL_LIBVIRT: runtime,
                    ResourceKind.REMOTE_LIBVIRT: runtime,
                }
            )
            resp = await _diagnostic_sysrq(
                pool,
                _ctx(Role.CONTRIBUTOR),
                system_id=str(system.id),
                command="show_memory",
                resolver=resolver,
            )
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "not_local_libvirt"

    asyncio.run(_run())


# --- control.watch_for_crash (#984, ADR-0367) -----------------------------------------


async def _watch_for_crash(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    deadline_s: float = 60.0,
    resolver: ProviderResolver | None = None,
) -> Any:
    return await control_tools.watch_for_crash_system(
        pool,
        ctx,
        system_id=system_id,
        deadline_s=deadline_s,
        resolver=resolver if resolver is not None else provider_resolver(),
        idempotency_key=None,
    )


def test_watch_for_crash_enqueues_job_for_contributor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _watch_for_crash(
                pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, deadline_s=45.0
            )
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT payload FROM jobs WHERE kind = 'watch_for_crash' AND dedup_key = %s",
                    (f"{sys_id}:watch_for_crash",),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["payload"]["deadline_s"] == 45.0

    asyncio.run(_run())


def test_watch_for_crash_is_capped_to_one_in_flight_per_system(migrated_url: str) -> None:
    # Stable per-System dedup key: a second watch while one is in flight returns the same job,
    # so a contributor cannot flood the shared worker lane with unbounded pure-wait jobs.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            first = await _watch_for_crash(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id)
            second = await _watch_for_crash(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id)
            assert first.object_id == second.object_id  # same job — in-flight cap
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'watch_for_crash' "
                    "AND dedup_key = %s",
                    (f"{sys_id}:watch_for_crash",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_watch_for_crash_re_issue_after_cancel_recycles(migrated_url: str) -> None:
    # A canceled watch must not wedge the stable dedup key: recycle_canceled lets a re-issue
    # reclaim the slot with a fresh queued watch, so cancel does not brick the tool on that System.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            first = await _watch_for_crash(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE jobs SET state = 'canceled' WHERE id = %s", (first.object_id,)
                )
            second = await _watch_for_crash(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id)
            assert second.status == "queued"  # fresh watch, not the dead canceled job
            assert second.object_id == first.object_id  # recycled in place (same row)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state FROM jobs WHERE dedup_key = %s", (f"{sys_id}:watch_for_crash",)
                )
                rows = await cur.fetchall()
        assert [r["state"] for r in rows] == ["queued"]  # one row, reclaimed

    asyncio.run(_run())


def test_watch_for_crash_clamps_deadline_above_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            await _watch_for_crash(
                pool,
                _ctx(Role.CONTRIBUTOR),
                system_id=sys_id,
                deadline_s=WATCH_MAX_DEADLINE_S + 999,
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT payload FROM jobs WHERE kind = 'watch_for_crash' AND dedup_key = %s",
                    (f"{sys_id}:watch_for_crash",),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["payload"]["deadline_s"] == WATCH_MAX_DEADLINE_S

    asyncio.run(_run())


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_watch_for_crash_bad_deadline_is_config_error(migrated_url: str, bad: float) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _watch_for_crash(
                pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, deadline_s=bad
            )
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_watch_for_crash_not_ready_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            resp = await _watch_for_crash(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id)
            assert resp.error_category == "configuration_error"
            assert resp.data["current_status"] == "provisioning"

    asyncio.run(_run())


def test_watch_for_crash_viewer_is_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await _watch_for_crash(pool, _ctx(Role.VIEWER), system_id=sys_id)

    asyncio.run(_run())


def test_watch_for_crash_unknown_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _watch_for_crash(pool, _ctx(Role.CONTRIBUTOR), system_id=str(uuid4()))
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- control.capture_traffic tool ----------------------------------------------------------


async def _seed_bound_run(pool: AsyncConnectionPool, sys_id: str) -> str:
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
                state=RunState.RUNNING,
                build_profile={},
            ),
        )
    return str(run.id)


async def _capture_traffic(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    resolver: ProviderResolver | None = None,
    duration_s: int = 30,
    max_bytes: int = 67108864,
    snaplen: int = 128,
    capture_filter: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    return await control_tools.capture_traffic_system(
        pool,
        ctx,
        resolver=resolver if resolver is not None else provider_resolver(),
        run_id=run_id,
        duration_s=duration_s,
        max_bytes=max_bytes,
        snaplen=snaplen,
        capture_filter=capture_filter,
        idempotency_key=idempotency_key,
    )


def test_capture_traffic_enqueues_job_for_contributor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_bound_run(pool, sys_id)
            resp = await _capture_traffic(
                pool, _ctx(Role.CONTRIBUTOR), run_id=run_id, capture_filter="tcp port 80"
            )
            assert resp.status == "queued"
            assert resp.data["run_id"] == run_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT payload FROM jobs WHERE kind = 'capture_traffic' AND dedup_key = %s",
                    (f"{run_id}:capture_traffic",),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["payload"]["capture_filter"] == "tcp port 80"
        assert row["payload"]["snaplen"] == 128

    asyncio.run(_run())


def test_capture_traffic_unbound_run_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            # A Run with no bound System: seed one against a system, then null its binding.
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_bound_run(pool, sys_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET system_id = NULL WHERE id = %s", (UUID(run_id),)
                )
            resp = await _capture_traffic(pool, _ctx(Role.CONTRIBUTOR), run_id=run_id)
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "run_unbound"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'capture_traffic'")
                count_row = await cur.fetchone()
                assert count_row is not None
                assert count_row["n"] == 0

    asyncio.run(_run())


def test_capture_traffic_non_ready_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            run_id = await _seed_bound_run(pool, sys_id)
            resp = await _capture_traffic(pool, _ctx(Role.CONTRIBUTOR), run_id=run_id)
            assert resp.error_category == "configuration_error"
            assert resp.data["current_status"] == "provisioning"

    asyncio.run(_run())


def test_capture_traffic_unsupported_provider_is_capability_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_bound_run(pool, sys_id)
            resp = await _capture_traffic(
                pool,
                _ctx(Role.CONTRIBUTOR),
                run_id=run_id,
                resolver=provider_resolver(supports_traffic_capture=False),
            )
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "capability_unsupported"
            assert resp.data["capability"] == "traffic_capture"

    asyncio.run(_run())


def test_capture_traffic_too_long_filter_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_bound_run(pool, sys_id)
            resp = await _capture_traffic(
                pool, _ctx(Role.CONTRIBUTOR), run_id=run_id, capture_filter="a" * 2000
            )
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "invalid_filter"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'capture_traffic'")
                count_row = await cur.fetchone()
                assert count_row is not None
                assert count_row["n"] == 0

    asyncio.run(_run())


def test_capture_traffic_viewer_is_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_bound_run(pool, sys_id)
            with pytest.raises(AuthorizationError):
                await _capture_traffic(pool, _ctx(Role.VIEWER), run_id=run_id)

    asyncio.run(_run())

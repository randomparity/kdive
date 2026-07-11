"""``ops.reconcile_now`` handler tests (#137, ADR-0062 §reconcile).

The handler is called directly with an injected pool and request context (the repo's
primary test contract). These tests prove the three acceptance criteria:

* a pending repair (an orphaned System) is resolved by one call, which returns a
  per-class summary, and the periodic loop's machinery is untouched;
* the on-demand pass shares the periodic reconciler's ``reconcile_once`` and its
  per-System advisory lock, so an on-demand pass and a concurrent periodic pass
  serialize on the same lock and cannot double-act on one object;
* ``platform_operator`` gating is enforced (a non-operator is denied and writes no
  ``platform_audit_log`` row).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops.reconcile import reconcile as ops_reconcile
from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler.loop import ALL_REPAIR_KINDS, ReconcileConfig, reconcile_once
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.store.assembly import optional_object_store
from tests.db_waits import wait_until_any_backend_waiting
from tests.reconciler.conftest import connect, seed_system


def _ctx(*, platform_roles: frozenset[PlatformRole] = frozenset()) -> RequestContext:
    return RequestContext(
        principal="op-1",
        agent_session="sess-1",
        projects=(),
        roles={},
        platform_roles=platform_roles,
    )


_OPERATOR = frozenset({PlatformRole.PLATFORM_OPERATOR})


def _ports() -> ops_reconcile.ReconcileRepairPorts:
    return ops_reconcile.ReconcileRepairPorts(reaper=NullReaper(), upload_store=None)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=5, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _teardown_job_count(url: str) -> int:
    async with await connect(url) as check:
        cur = await check.execute("SELECT count(*) FROM jobs WHERE kind = 'teardown'")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _platform_audit_count(url: str) -> int:
    async with await connect(url) as check:
        cur = await check.execute(
            "SELECT count(*) FROM platform_audit_log WHERE tool = 'ops.reconcile_now'"
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    async with await connect(url) as check:
        cur = await check.execute(
            "SELECT principal, platform_role, scope FROM platform_audit_log "
            "WHERE tool = 'ops.reconcile_now'"
        )
        return await cur.fetchall()


def test_reconcile_now_resolves_orphaned_system_and_returns_summary(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool, _ctx(platform_roles=_OPERATOR), ports=_ports()
            )
        assert resp.status == "ok"
        assert resp.data["orphaned_systems"] == 1
        repair_counts = resp.data["repair_counts"]
        assert isinstance(repair_counts, dict)
        assert repair_counts["orphaned_systems"] == 1
        assert resp.data["failures"] == ""
        # The pending repair was actually performed, not just counted.
        assert await _teardown_job_count(migrated_url) == 1
        # The action was audited to platform_audit_log with the caller's held roles.
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "all-projects")]

    asyncio.run(_run())


def test_audit_records_all_held_platform_roles(migrated_url: str) -> None:
    # The audit row reflects the roles the caller actually holds, not the gate literal —
    # an operator who also holds auditor records both (sorted, comma-joined).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool,
                _ctx(
                    platform_roles=frozenset(
                        {PlatformRole.PLATFORM_OPERATOR, PlatformRole.PLATFORM_AUDITOR}
                    )
                ),
                ports=_ports(),
            )
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_auditor,platform_operator", "all-projects")]

    asyncio.run(_run())


def test_reconcile_now_clean_state_returns_zero_summary(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool, _ctx(platform_roles=_OPERATOR), ports=_ports()
            )
        assert resp.status == "ok"
        assert resp.data["orphaned_systems"] == 0
        assert resp.data["expired_allocations"] == 0
        assert resp.data["reaped_active_allocations"] == 0
        assert resp.data["promoted_allocations"] == 0
        assert resp.data["queue_timeouts"] == 0
        assert resp.data["reconciled_inventory"] == 0
        repair_counts = resp.data["repair_counts"]
        assert isinstance(repair_counts, dict)
        assert tuple(repair_counts) == ALL_REPAIR_KINDS
        assert all(repair_counts[repair_kind] == 0 for repair_kind in ALL_REPAIR_KINDS)
        assert resp.data["failures"] == ""
        # A pass with nothing to repair is still audited (it ran a control action).
        assert await _platform_audit_count(migrated_url) == 1

    asyncio.run(_run())


def test_project_only_non_operator_is_denied_and_writes_no_audit_row(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(pool, _ctx(), ports=_ports())
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            assert resp.suggested_next_actions == ["ops.reconcile_now"]
        # The denied calls performed no repair and wrote no audit row.
        assert await _teardown_job_count(migrated_url) == 0
        assert await _platform_audit_count(migrated_url) == 0

    asyncio.run(_run())


def test_auditor_non_operator_denial_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool,
                _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR})),
                ports=_ports(),
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
        assert await _teardown_job_count(migrated_url) == 0
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_auditor", "all-projects")]

    asyncio.run(_run())


def test_admin_does_not_satisfy_operator(migrated_url: str) -> None:
    # ADR-0043 §2: platform_admin implies only platform_auditor, never platform_operator;
    # operator gating is its own axis, so an admin-only token is denied this operator tool.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool,
                _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN})),
                ports=_ports(),
            )
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_on_demand_pass_serializes_with_periodic_on_the_same_system_lock(
    migrated_url: str,
) -> None:
    """An on-demand pass blocks on the per-System advisory lock a periodic pass holds.

    Holding the per-System lock (the lock ``_repair_orphaned_systems`` takes) externally
    must stall the orphaned-System repair inside ``reconcile_now`` — proving the on-demand
    pass runs the same advisory-locked code path, not a second lock-free one. Released, the
    repair then proceeds and enqueues exactly one teardown.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool, pool.connection() as holder:
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.SYSTEM, system_id),
            ):
                task = asyncio.create_task(
                    ops_reconcile.reconcile_now(
                        pool, _ctx(platform_roles=_OPERATOR), ports=_ports()
                    )
                )
                await wait_until_any_backend_waiting(holder, locktype="advisory")
                assert not task.done(), "reconcile_now did not block on the held System lock"
                assert await _teardown_job_count(migrated_url) == 0
            # holder transaction committed -> lock released; the repair now proceeds.
            resp = await task
        assert resp.status == "ok"
        assert resp.data["orphaned_systems"] == 1
        assert await _teardown_job_count(migrated_url) == 1

    asyncio.run(_run())


def test_concurrent_on_demand_and_periodic_pass_enqueue_one_teardown(migrated_url: str) -> None:
    """A concurrent on-demand + periodic pass on one orphaned System enqueue one teardown.

    Both passes call the same ``reconcile_once``. The single-teardown outcome here is
    carried by the ``{system_id}:teardown`` dedup key; the advisory-lock *serialization*
    that prevents double-acting is proven separately by
    ``test_on_demand_pass_serializes_with_periodic_on_the_same_system_lock`` (a held lock
    actually stalls the on-demand repair). Together they show concurrent passes neither
    double-enqueue nor run the repair lock-free.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            on_demand = ops_reconcile.reconcile_now(
                pool, _ctx(platform_roles=_OPERATOR), ports=_ports()
            )
            periodic = reconcile_once(pool, NullReaper(), config=ReconcileConfig())
            results = await asyncio.gather(on_demand, periodic)
        assert results[0].status == "ok"
        assert await _teardown_job_count(migrated_url) == 1

    asyncio.run(_run())


class _FakeFastMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[[], Awaitable[ToolResponse]]] = {}

    def tool(
        self,
        *,
        name: str,
        annotations: object,
        meta: object,
    ) -> Callable[[Callable[[], Awaitable[ToolResponse]]], Callable[[], Awaitable[ToolResponse]]]:
        del annotations, meta

        def _decorate(
            function: Callable[[], Awaitable[ToolResponse]],
        ) -> Callable[[], Awaitable[ToolResponse]]:
            self.tools[name] = function
            return function

        return _decorate


def test_register_forwards_repair_ports_to_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        app = _FakeFastMCP()
        pool = cast(AsyncConnectionPool, object())
        ctx = _ctx(platform_roles=_OPERATOR)
        ports = _ports()
        captured: dict[str, Any] = {}

        async def _fake_reconcile_now(
            handler_pool: AsyncConnectionPool,
            handler_ctx: RequestContext,
            *,
            ports: ops_reconcile.ReconcileRepairPorts,
        ) -> ToolResponse:
            captured["pool"] = handler_pool
            captured["ctx"] = handler_ctx
            captured["ports"] = ports
            return ToolResponse.success("reconcile", "ok")

        monkeypatch.setattr(ops_reconcile, "current_context", lambda: ctx)
        monkeypatch.setattr(ops_reconcile, "reconcile_now", _fake_reconcile_now)

        ops_reconcile.register(cast(FastMCP, app), pool, ports=ports)

        resp = await app.tools["ops.reconcile_now"]()
        assert resp.status == "ok"
        assert captured == {"pool": pool, "ctx": ctx, "ports": ports}

    asyncio.run(_run())


@pytest.mark.usefixtures("migrated_url")
def test_register_resolves_upload_store_off_without_s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirrors the periodic loop: no KDIVE_S3_* env -> the upload reaper stays off (None),
    # rather than raising, so the on-demand pass repairs the same set as the periodic one.
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)
    assert optional_object_store() is None


@pytest.mark.usefixtures("migrated_url")
def test_register_reraises_partial_s3_config() -> None:
    try:
        config.load({"KDIVE_S3_ENDPOINT_URL": "http://localhost:9000"})
        with pytest.raises(CategorizedError):
            optional_object_store()
    finally:
        config.reset()

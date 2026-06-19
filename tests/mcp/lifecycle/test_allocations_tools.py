"""allocations.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS
from kdive.domain.accounting import Budget, Quota
from kdive.domain.capacity.state import AllocationState, IllegalTransition
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.mcp.tools.lifecycle.allocations.common import _envelope_for_allocation
from kdive.mcp.tools.lifecycle.allocations.lifecycle import (
    ReleaseOutcome,
    RenewOutcome,
    _release_response,
    _renew_response,
    release_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.request import request_allocation
from kdive.mcp.tools.lifecycle.allocations.view import (
    get_allocation,
    list_allocations,
    wait_allocation,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.resources.discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _register(
    pool: AsyncConnectionPool, *, cap: int = 1, limit: str = "1000000", quota: int = 1_000_000
) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        # The M1 gate denies a project with no budget/quota row; seed generous rows so the
        # host cap (or the explicit test budget) is the binding constraint.
        await BUDGETS.upsert(
            conn,
            Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=quota,
                max_concurrent_systems=quota,
                updated_at=_DT,
            ),
        )
    return str(res.id)


async def _request(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str = "proj",
    vcpus: int | None = 1,
    memory_gb: int | None = 0,
    disk_gb: int | None = 10,
    shape: str | None = None,
    window: object | None = None,
    idempotency_key: str | None = None,
    kind: str = "local-libvirt",
) -> ToolResponse:
    request: dict[str, object] = {
        "window": window,
        "resource": {"mode": "kind", "kind": kind},
    }
    if shape is not None:
        request["shape"] = shape
    else:
        request["vcpus"] = vcpus
        request["memory_gb"] = memory_gb
        request["disk_gb"] = disk_gb
    return await request_allocation(
        pool,
        ctx,
        project=project,
        request=AllocationRequestPayload.model_validate(request),
        idempotency_key=idempotency_key,
    )


async def _request_by_id(
    pool: AsyncConnectionPool, ctx: RequestContext, resource_id: str, *, project: str = "proj"
) -> ToolResponse:
    return await request_allocation(
        pool,
        ctx,
        project=project,
        request=AllocationRequestPayload.model_validate(
            {
                "vcpus": 1,
                "memory_gb": 0,
                "disk_gb": 10,
                "window": None,
                "resource": {"mode": "id", "resource_id": resource_id},
            }
        ),
    )


async def _request_by_pool(
    pool: AsyncConnectionPool, ctx: RequestContext, target_pool: str, *, project: str = "proj"
) -> ToolResponse:
    return await request_allocation(
        pool,
        ctx,
        project=project,
        request=AllocationRequestPayload.model_validate(
            {
                "vcpus": 1,
                "memory_gb": 0,
                "disk_gb": 10,
                "window": None,
                "resource": {"mode": "pool", "pool": target_pool},
            }
        ),
    )


async def _set_resource_flags(
    pool: AsyncConnectionPool,
    resource_id: str,
    *,
    cordoned: bool | None = None,
    status: str | None = None,
) -> None:
    async with pool.connection() as conn:
        if cordoned is not None:
            await conn.execute(
                "UPDATE resources SET cordoned = %s WHERE id = %s", (cordoned, UUID(resource_id))
            )
        if status is not None:
            await conn.execute(
                "UPDATE resources SET status = %s WHERE id = %s", (status, UUID(resource_id))
            )


async def _seed_alloc(pool: AsyncConnectionPool, resource_id: str, state: AllocationState) -> str:
    # A queued `requested` row holds no host: resource_id must be NULL (the 0016 CHECK).
    placed = state is not AllocationState.REQUESTED
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=UUID(resource_id) if placed else None,
                state=state,
            ),
        )
    return str(alloc.id)


async def _seed_requested(
    pool: AsyncConnectionPool,
    *,
    created_at: datetime,
    kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
    resource_id: str | None = None,
) -> str:
    # by-id when resource_id is given (requested_resource_id set, requested_kind NULL);
    # otherwise by-kind (requested_kind set). Mirrors how a real queued row is shaped.
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=created_at,
                updated_at=created_at,
                principal="user-1",
                project="proj",
                resource_id=None,
                state=AllocationState.REQUESTED,
                requested_kind=None if resource_id is not None else kind,
                requested_resource_id=UUID(resource_id) if resource_id is not None else None,
            ),
        )
    return str(alloc.id)


def test_request_under_cap_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            resp = await _request(pool, _ctx())
        assert resp.status == "granted"
        assert resp.error_category is None
        assert resp.data["project"] == "proj"
        # #462: a granted allocation points the agent at the create-a-VM next step.
        assert "systems.provision" in resp.suggested_next_actions

    asyncio.run(_run())


def test_request_at_cap_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=1)
            await _request(pool, _ctx())
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "allocation_denied"
        assert resp.object_id == res_id
        assert resp.data["reason"] == "at_capacity"

    asyncio.run(_run())


def test_request_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            try:
                await _request(pool, _ctx(Role.VIEWER))
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_request_no_resource_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_request_no_resource_empty_fleet_detail_and_actions(migrated_url: str) -> None:
    # #471: a by-kind denial on an empty fleet names the missing kind and points at discovery.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _request(pool, _ctx())
        assert resp.error_category == "configuration_error"
        assert resp.detail is not None
        assert "local-libvirt" in resp.detail
        assert "no resource kinds are registered" in resp.detail
        assert "resources.list" in resp.suggested_next_actions
        assert "shapes.list" in resp.suggested_next_actions

    asyncio.run(_run())


def test_request_unregistered_kind_lists_available_kinds(migrated_url: str) -> None:
    # #471: with one kind registered, a denial for a DIFFERENT kind names the available kind.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)  # registers a local-libvirt host
            resp = await _request(pool, _ctx(), kind="fault-inject")
        assert resp.error_category == "configuration_error"
        assert resp.detail is not None
        assert "fault-inject" in resp.detail
        assert "available kinds: local-libvirt" in resp.detail
        assert "resources.list" in resp.suggested_next_actions

    asyncio.run(_run())


def test_request_unknown_id_detail_names_id_not_kinds(migrated_url: str) -> None:
    # #471: a by-id denial names the id (caller-supplied) and does NOT enumerate kinds.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            missing = str(uuid4())
            resp = await _request_by_id(pool, _ctx(), missing)
        assert resp.error_category == "configuration_error"
        assert resp.detail is not None
        assert missing in resp.detail
        assert "available kinds" not in resp.detail
        assert "resources.list" in resp.suggested_next_actions

    asyncio.run(_run())


def test_request_by_pool_grants_from_pool_member(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)  # registers a host in pool 'local-libvirt'
            return await _request_by_pool(pool, _ctx(), "local-libvirt")

    resp = asyncio.run(_run())
    assert resp.error_category is None
    assert resp.status == "granted"


def test_request_unknown_pool_denial_does_not_leak_pool_names(migrated_url: str) -> None:
    # ADR-0186: a by-pool denial names the requested pool only — it must not enumerate other
    # (possibly other-tenant) pool names like 'local-libvirt'.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)  # a host exists in pool 'local-libvirt'
            return await _request_by_pool(pool, _ctx(), "customer-x-private")

    resp = asyncio.run(_run())
    assert resp.error_category == "configuration_error"
    assert resp.detail is not None
    assert "customer-x-private" in resp.detail
    assert "local-libvirt" not in resp.detail
    assert "available kinds" not in resp.detail


def test_denial_envelope_guides_agent_to_a_grant(migrated_url: str) -> None:
    # #471 acceptance: a black-box agent following only the envelope reaches a grant. The first
    # default request is denied with discovery actions; after the agent "discovers" the registered
    # kind (resources.list would surface local-libvirt), it retries that kind and is granted.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            denied = await _request(pool, _ctx(), kind="fault-inject")
            assert denied.error_category == "configuration_error"
            assert "resources.list" in denied.suggested_next_actions
            # The detail names the available kind; the agent retries with it.
            assert "local-libvirt" in (denied.detail or "")
            return await _request(pool, _ctx(), kind="local-libvirt")

    granted = asyncio.run(_run())
    assert granted.status == "granted"


def test_capacity_denial_detail_is_prose_not_token(migrated_url: str) -> None:
    # #471: a host-cap denial carries human prose (not the raw `at_capacity` token) and keeps
    # its queue/wait recourse action.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=1)
            await _request(pool, _ctx())
            resp = await _request(pool, _ctx())
        assert resp.error_category == "allocation_denied"
        assert resp.detail is not None
        assert "capacity" in resp.detail.lower()
        assert resp.detail != "at_capacity"
        assert resp.suggested_next_actions == ["allocations.list"]
        # The structured reason token stays in `data` for machine consumers.
        assert resp.data["reason"] == "at_capacity"

    asyncio.run(_run())


def test_get_own_allocation_returns_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            resp = await get_allocation(pool, _ctx(), req.object_id)
        assert resp.object_id == req.object_id
        assert resp.status == "granted"

    asyncio.run(_run())


def test_get_allocation_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            with pytest.raises(AuthorizationError):
                await get_allocation(pool, _ctx(role=None), req.object_id)

    asyncio.run(_run())


def test_get_other_project_allocation_is_not_found(migrated_url: str) -> None:
    # No-leak (ADR-0097): an ungranted by-id lookup is not_found, NEVER authorization_denied —
    # the membership-envelope change (ADR-0098) flips require_project for NAMED-scope tools but
    # the by-id getters never call require_project, so this path is structurally untouched.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            other = _ctx(projects=("elsewhere",), role=Role.OPERATOR)
            resp = await get_allocation(pool, other, req.object_id)
        assert resp.status == "error"
        assert resp.error_category == "not_found"
        assert resp.error_category != "authorization_denied"

    asyncio.run(_run())


def test_get_absent_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await get_allocation(pool, _ctx(), str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_get_malformed_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await get_allocation(pool, _ctx(), "nope")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # ADR-0174: actionable reason + non-null detail for the malformed-id parse failure.
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "nope" in resp.detail

    asyncio.run(_run())


def test_get_ungranted_envelope_matches_absent(migrated_url: str) -> None:
    # The no-leak invariant (ADR-0020/0097): an allocation in a project the caller cannot see
    # must be indistinguishable from a genuinely-absent one. Same category, same data — only
    # the echoed object_id (the input id) differs, which carries no membership signal.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            other = _ctx(projects=("elsewhere",), role=Role.OPERATOR)
            ungranted = await get_allocation(pool, other, req.object_id)
            absent = await get_allocation(pool, other, str(uuid4()))
        assert ungranted.status == absent.status == "error"
        assert ungranted.error_category == absent.error_category == "not_found"
        assert ungranted.data == absent.data
        assert ungranted.suggested_next_actions == absent.suggested_next_actions

    asyncio.run(_run())


def test_get_failed_allocation_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.FAILED)
            resp = await get_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_release_granted_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            resp = await release_allocation(pool, _ctx(), req.object_id)
            assert resp.status == "released"
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (req.object_id,)
                )
                row = await cur.fetchone()
            # ->granted (admission) + granted->releasing + releasing->released
            assert row is not None and row[0] == 3

    asyncio.run(_run())


def test_release_active_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.ACTIVE)
            resp = await release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "released"

    asyncio.run(_run())


def test_release_absent_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await release_allocation(pool, _ctx(), str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_release_ungranted_allocation_is_not_found(migrated_url: str) -> None:
    # No-leak: releasing another project's allocation is indistinguishable from absent.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            other = _ctx(projects=("elsewhere",), role=Role.OPERATOR)
            resp = await release_allocation(pool, other, req.object_id)
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_release_malformed_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await release_allocation(pool, _ctx(), "nope")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_release_terminal_allocation_is_stale_handle(migrated_url: str) -> None:
    # A terminal allocation was already reconciled (by a prior release or the ->expired
    # sweep); re-releasing it is a stale handle, not a config error (ADR-0040 §4).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.RELEASED)
            resp = await release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "stale_handle"
        assert resp.data["current_status"] == "released"

    asyncio.run(_run())


def test_release_requested_allocation_cancels_with_no_credit(migrated_url: str) -> None:
    # A queued `requested` row was never reserved (ADR-0069): release cancels it directly to
    # `released` — no `releasing` hop, no ledger credit, no active_ended_at stamp.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.REQUESTED)
            resp = await release_allocation(pool, _ctx(), alloc_id)
            assert resp.status == "released"
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT state, active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM ledger WHERE allocation_id = %s", (alloc_id,)
                )
                ledger = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (alloc_id,)
                )
                audit = await cur.fetchone()
            assert row is not None and row[0] == "released" and row[1] is None
            assert ledger is not None and ledger[0] == 0  # never reserved → no credit
            # Exactly one audit row: requested->released (no releasing hop).
            assert audit is not None and audit[0] == 1

    asyncio.run(_run())


def test_release_illegal_transition_backstop_returns_failure(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the backstop: a state change slips past the locked re-read (a future
    # provision path could do this). update_state raising IllegalTransition must map to a
    # clean configuration_error envelope carrying the actual current state, not a 500.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.GRANTED)

            async def _boom(*args: object, **kwargs: object) -> object:
                raise IllegalTransition("forced")

            monkeypatch.setattr(ALLOCATIONS, "update_state", _boom)
            resp = await release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "granted"  # re-read on a fresh connection

    asyncio.run(_run())


def test_release_response_includes_service_error_details() -> None:
    uid = uuid4()
    outcome = ReleaseOutcome(
        released=False,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": "state"},
    )

    resp = _release_response(uid, outcome)

    assert resp.data["field"] == "state"


def test_renew_response_includes_service_error_details() -> None:
    uid = uuid4()
    outcome = RenewOutcome(
        renewed=False,
        allocation=None,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"window": "0"},
    )

    resp = _renew_response(uid, outcome)

    assert resp.data["window"] == "0"


def test_list_returns_project_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=3)
            await _request(pool, _ctx())
            await _request(pool, _ctx())
            responses = await list_allocations(pool, _ctx(), project="proj", limit=50)
        items = responses.items
        assert responses.object_id == "allocations"
        assert responses.status == "ok"
        assert responses.data["project"] == "proj"
        assert len(items) == 2
        assert all(r.status == "granted" for r in items)

    asyncio.run(_run())


def test_list_allocations_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=1)
            await _request(pool, _ctx())
            with pytest.raises(AuthorizationError):
                await list_allocations(pool, _ctx(role=None), project="proj", limit=50)

    asyncio.run(_run())


def test_pick_by_kind_skips_cordoned_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, cordoned=True)
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_pick_by_kind_skips_non_available_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, status="degraded")
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_explicit_id_naming_cordoned_host_is_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, cordoned=True)
            resp = await _request_by_id(pool, _ctx(), res_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_explicit_id_naming_non_available_host_is_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, status="offline")
            resp = await _request_by_id(pool, _ctx(), res_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_explicit_id_naming_schedulable_host_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            resp = await _request_by_id(pool, _ctx(), res_id)
        assert resp.status == "granted"
        assert resp.data["resource_id"] == res_id

    asyncio.run(_run())


def test_existing_allocations_untouched_when_host_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            granted = await _request(pool, _ctx())
            await _set_resource_flags(pool, res_id, cordoned=True)
            # The existing allocation is still readable and unchanged; cordon only gates
            # new placement, never live allocations.
            existing = await get_allocation(pool, _ctx(), granted.object_id)
        assert existing.object_id == granted.object_id
        assert existing.status == "granted"

    asyncio.run(_run())


def test_uncordon_restores_both_placement_paths(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=4)
            await _set_resource_flags(pool, res_id, cordoned=True)
            assert (await _request(pool, _ctx())).status == "error"
            assert (await _request_by_id(pool, _ctx(), res_id)).status == "error"
            await _set_resource_flags(pool, res_id, cordoned=False)
            by_kind = await _request(pool, _ctx())
            by_id = await _request_by_id(pool, _ctx(), res_id)
        assert by_kind.status == "granted"
        assert by_id.status == "granted"

    asyncio.run(_run())


# --- M1.4 shape selector (#161) -------------------------------------------------------

# The seed shapes (migration 0013): name -> (vcpus, memory_mb, disk_gb).
_SEED_SHAPES = {
    "small": (1, 1024, 10),
    "medium": (2, 4096, 20),
    "large": (4, 8192, 40),
    "max": (8, 16384, 80),
}


async def _fetch_alloc(pool: AsyncConnectionPool, alloc_id: str) -> Allocation:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, UUID(alloc_id))
    assert alloc is not None
    return alloc


def test_shape_request_persists_resolved_tuple_and_shape_label(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), shape="medium", window=1)
            assert resp.status == "granted"
            alloc = await _fetch_alloc(pool, resp.object_id)
        # medium = 2 vcpu / 4096 MB / 20 GB; memory_mb -> memory_gb is lossless.
        assert alloc.requested_vcpus == 2
        assert alloc.requested_memory_gb == 4
        assert alloc.requested_disk_gb == 20
        assert alloc.shape == "medium"

    asyncio.run(_run())


def test_custom_request_records_null_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), vcpus=2, memory_gb=4, disk_gb=20, window=1)
            assert resp.status == "granted"
            alloc = await _fetch_alloc(pool, resp.object_id)
        assert alloc.shape is None
        assert alloc.requested_disk_gb == 20

    asyncio.run(_run())


def test_unknown_shape_fails_closed_with_no_write(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), shape="gpu-xl", window=1)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                count = await conn.execute("SELECT count(*) FROM allocations")
                row = await count.fetchone()
        assert row is not None and row[0] == 0

    asyncio.run(_run())


def test_over_host_shape_fails_closed_with_no_durable_write(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            # Add a shape larger than the fake host (8 vcpu / 16384 MB).
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) "
                    "VALUES ('huge', 64, 131072, 500)"
                )
            resp = await _request(pool, _ctx(), shape="huge", window=1)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                for table in ("allocations", "ledger", "audit_log"):
                    row = await (await conn.execute(f"SELECT count(*) FROM {table}")).fetchone()
                    assert row is not None and row[0] == 0, table

    asyncio.run(_run())


def test_failed_envelope_reports_failure_category_else_infrastructure() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from kdive.domain.capacity.state import AllocationState
    from kdive.domain.errors import ErrorCategory
    from kdive.domain.models import Allocation

    _id = uuid4()
    _now = datetime.now(UTC)

    def _make(failure_category: ErrorCategory | None = None) -> Allocation:
        return Allocation(
            id=_id,
            created_at=_now,
            updated_at=_now,
            principal="p",
            agent_session="s",
            project="proj",
            state=AllocationState.FAILED,
            failure_category=failure_category,
        )

    # NULL cause -> the unchanged infrastructure_failure fallback.
    null_cause = _envelope_for_allocation(_make())
    assert null_cause.error_category == ErrorCategory.INFRASTRUCTURE_FAILURE.value
    assert null_cause.retryable is True
    # A budget terminate -> allocation_denied, terminal.
    budget = _envelope_for_allocation(_make(ErrorCategory.ALLOCATION_DENIED))
    assert budget.error_category == ErrorCategory.ALLOCATION_DENIED.value
    assert budget.retryable is False
    # A queue timeout -> queue_timeout, retryable.
    timed_out = _envelope_for_allocation(_make(ErrorCategory.QUEUE_TIMEOUT))
    assert timed_out.error_category == ErrorCategory.QUEUE_TIMEOUT.value
    assert timed_out.retryable is True


def test_queue_position_counts_same_kind_fifo(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            a = await _seed_requested(pool, created_at=datetime(2026, 1, 1, tzinfo=UTC))
            b = await _seed_requested(pool, created_at=datetime(2026, 1, 2, tzinfo=UTC))
            c = await _seed_requested(pool, created_at=datetime(2026, 1, 3, tzinfo=UTC))
            ra = await get_allocation(pool, _ctx(), a)
            rb = await get_allocation(pool, _ctx(), b)
            rc = await get_allocation(pool, _ctx(), c)
        assert (ra.data["queue_position"], ra.data["queue_ahead"]) == (1, 0)
        assert (rb.data["queue_position"], rb.data["queue_ahead"]) == (2, 1)
        assert (rc.data["queue_position"], rc.data["queue_ahead"]) == (3, 2)
        # #462: a queued (requested) allocation holds no host yet, so it does not advertise
        # systems.provision (no allocation to provision onto until it is promoted to granted).
        assert "systems.provision" not in ra.suggested_next_actions

    asyncio.run(_run())


def test_queue_position_scoped_to_same_target(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool)  # a real resource id to be the by-id target
            # Two by-kind rows ahead in time, plus one later by-id row. The by-id row's
            # position counts only same-requested_resource_id rows, so the by-kind rows
            # (requested_resource_id NULL) do not shift it.
            await _seed_requested(pool, created_at=datetime(2026, 1, 1, tzinfo=UTC))
            await _seed_requested(pool, created_at=datetime(2026, 1, 2, tzinfo=UTC))
            by_id = await _seed_requested(
                pool, created_at=datetime(2026, 1, 3, tzinfo=UTC), resource_id=res
            )
            r = await get_allocation(pool, _ctx(), by_id)
        assert r.data["queue_position"] == 1

    asyncio.run(_run())


def test_queue_position_absent_on_granted_and_in_list(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool)
            granted = await _seed_alloc(pool, res, AllocationState.GRANTED)
            rg = await get_allocation(pool, _ctx(), granted)
            rl = await list_allocations(pool, _ctx(), project="proj", limit=50)
        assert "queue_position" not in rg.data
        assert all("queue_position" not in item.data for item in rl.items)
        # #462: reaching a granted allocation via allocations.get also advertises systems.provision.
        assert "systems.provision" in rg.suggested_next_actions

    asyncio.run(_run())


async def _force_grant(pool: AsyncConnectionPool, alloc_id: str, resource_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE allocations SET state = 'granted', resource_id = %s WHERE id = %s",
            (UUID(resource_id), UUID(alloc_id)),
        )


def test_wait_returns_immediately_when_already_settled(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool)
            granted = await _seed_alloc(pool, res, AllocationState.GRANTED)
            resp = await wait_allocation(pool, _ctx(), granted, timeout_s=5.0)
        assert resp.status == "granted"

    asyncio.run(_run())


def test_wait_returns_on_requested_to_granted_transition(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool)
            queued = await _seed_requested(pool, created_at=datetime(2026, 1, 1, tzinfo=UTC))
            flipped: dict[str, bool] = {"done": False}

            async def _sleep(_delay: float) -> None:
                if not flipped["done"]:
                    await _force_grant(pool, queued, res)
                    flipped["done"] = True
                await asyncio.sleep(0)

            resp = await wait_allocation(pool, _ctx(), queued, timeout_s=5.0, sleep=_sleep)
        assert resp.status == "granted"

    asyncio.run(_run())


def test_wait_returns_current_envelope_at_deadline_while_requested(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            queued = await _seed_requested(pool, created_at=datetime(2026, 1, 1, tzinfo=UTC))
            resp = await wait_allocation(pool, _ctx(), queued, timeout_s=0.0)
        assert resp.status == "requested"
        assert resp.data["queue_position"] == 1

    asyncio.run(_run())


def test_wait_not_found_for_absent_and_malformed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            absent = await wait_allocation(pool, _ctx(), str(uuid4()), timeout_s=0.0)
            bad = await wait_allocation(pool, _ctx(), "not-a-uuid", timeout_s=0.0)
        assert absent.error_category == "not_found"
        assert bad.error_category == "configuration_error"
        # ADR-0174: the malformed-id branch is actionable; the no-leak not_found stays bare.
        assert bad.data["reason"] == "invalid_uuid"
        assert absent.detail == "not found" and "reason" not in absent.data

    asyncio.run(_run())


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), float("-inf")])
def test_wait_non_finite_timeout_is_configuration_error(
    migrated_url: str, timeout_s: float
) -> None:
    # Mirrors jobs.wait's guard: a non-finite timeout never becomes a deadline.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            queued = await _seed_requested(pool, created_at=datetime(2026, 1, 1, tzinfo=UTC))
            resp = await wait_allocation(pool, _ctx(), queued, timeout_s=timeout_s)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # ADR-0174: a non-finite timeout names its own reason.
        assert resp.data["reason"] == "invalid_timeout"
        assert resp.detail is not None

    asyncio.run(_run())


def test_envelope_surfaces_recovery_context_on_granted() -> None:
    res = uuid4()
    alloc = Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        resource_id=res,
        state=AllocationState.GRANTED,
        requested_kind=ResourceKind.LOCAL_LIBVIRT,
        requested_vcpus=4,
        requested_memory_gb=8,
        requested_disk_gb=40,
        shape="small",
    )
    data = _envelope_for_allocation(alloc).data
    assert data["resource_id"] == str(res)
    assert data["requested_kind"] == ResourceKind.LOCAL_LIBVIRT.value
    assert data["requested_vcpus"] == 4
    assert data["requested_memory_gb"] == 8
    assert data["requested_disk_gb"] == 40
    assert data["shape"] == "small"
    assert data["created_at"] == _DT.isoformat()
    assert data["requested_pcie_specs"] == []
    assert data["lease_expiry"] is None


def test_envelope_surfaces_selector_on_failed() -> None:
    alloc = Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        resource_id=None,
        state=AllocationState.FAILED,
        requested_kind=ResourceKind.LOCAL_LIBVIRT,
        failure_category=ErrorCategory.ALLOCATION_DENIED,
    )
    resp = _envelope_for_allocation(alloc)
    assert resp.status == "error"
    assert resp.data["requested_kind"] == ResourceKind.LOCAL_LIBVIRT.value
    assert resp.data["resource_id"] is None


def test_shapes_set_after_stamping_does_not_resize_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), shape="medium", window=1)
            assert resp.status == "granted"
            # Redefine `medium` in the catalog AFTER the allocation is stamped.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE system_shapes SET vcpus = 8, memory_mb = 16384, disk_gb = 80 "
                    "WHERE name = 'medium'"
                )
            alloc = await _fetch_alloc(pool, resp.object_id)
        # The stamped snapshot is unchanged — the catalog edit is not retroactive.
        assert alloc.requested_vcpus == 2
        assert alloc.requested_memory_gb == 4
        assert alloc.requested_disk_gb == 20
        assert alloc.shape == "medium"

    asyncio.run(_run())

"""allocations.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS
from kdive.db.resource_discovery import register_discovered_resource
from kdive.domain.accounting import Budget, Quota
from kdive.domain.capacity.state import AllocationState, IllegalTransition
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.mcp.tools.lifecycle.allocations.common import envelope_for_allocation
from kdive.mcp.tools.lifecycle.allocations.lifecycle import (
    ReleaseOutcome,
    RenewOutcome,
    _release_response,
    _renew_response,
    release_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.request import (
    _denial_detail,
    _denial_next_actions,
    _funding_denial_detail,
    request_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.view import (
    get_allocation,
    list_allocations,
    wait_allocation,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.allocation.admission.core import (
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
)
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


def _gate_entry(resp: ToolResponse, gate: str) -> Mapping[str, object]:
    """Return the named gate's entry from a denial's data["unmet"] (#833)."""
    unmet = resp.data["unmet"]
    assert isinstance(unmet, list)
    for entry in unmet:
        if isinstance(entry, dict) and entry.get("gate") == gate:
            return entry
    raise AssertionError(f"no {gate!r} entry in unmet: {unmet!r}")


def _unmet_gates(resp: ToolResponse) -> list[str]:
    """The ordered gate discriminators from a denial's data["unmet"] (#833)."""
    unmet = resp.data["unmet"]
    assert isinstance(unmet, list)
    gates: list[str] = []
    for entry in unmet:
        assert isinstance(entry, dict)
        gate = entry["gate"]
        assert isinstance(gate, str)
        gates.append(gate)
    return gates


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


def test_request_grant_drops_systems_provision_for_contributor(migrated_url: str) -> None:
    # #862/ADR-0261: a contributor's granted request must not be pointed at operator-only
    # systems.provision (allocations.request needs only contributor).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            resp = await _request(pool, _ctx(role=Role.CONTRIBUTOR))
        assert resp.status == "granted"
        assert "systems.provision" not in resp.suggested_next_actions
        assert resp.suggested_next_actions == ["allocations.get", "allocations.release"]

    asyncio.run(_run())


def test_get_granted_filters_next_actions_by_role(migrated_url: str) -> None:
    # #862/ADR-0261: the same filter applies on the read side.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            granted = await _request(pool, _ctx(role=Role.OPERATOR))
            alloc_id = granted.object_id
            viewer = await get_allocation(pool, _ctx(role=Role.VIEWER), alloc_id)
            contributor = await get_allocation(pool, _ctx(role=Role.CONTRIBUTOR), alloc_id)
            operator = await get_allocation(pool, _ctx(role=Role.OPERATOR), alloc_id)
        assert viewer.suggested_next_actions == ["allocations.get"]
        assert contributor.suggested_next_actions == ["allocations.get", "allocations.release"]
        assert "systems.provision" in operator.suggested_next_actions

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
        # #801: a host-cap denial is NOT a quota/budget funding problem — it must not name an
        # accounting remedy tool (the quota/budget branch does not leak into other categories).
        assert "accounting." not in resp.detail
        # The structured reason token stays in `data` for machine consumers.
        assert resp.data["reason"] == "at_capacity"

    asyncio.run(_run())


def test_quota_denial_names_set_quota_remedy_for_admin(migrated_url: str) -> None:
    # #801/ADR-0245: a fresh project's concurrency-quota denial points at the admin tool that
    # resolves it (accounting.set_quota), not just the empty allocations.list. #841: the tool is
    # led with only for an admin caller, who can actually invoke it.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, quota=0)  # quota row present but exhausted at 0
            return await _request(pool, _ctx(Role.ADMIN))

    resp = asyncio.run(_run())
    assert resp.error_category == "quota_exceeded"
    assert resp.suggested_next_actions[0] == "accounting.set_quota"
    assert "allocations.list" in resp.suggested_next_actions
    assert resp.detail is not None and "accounting.set_quota" in resp.detail


def test_quota_denial_omits_admin_tool_for_non_admin(migrated_url: str) -> None:
    # #841: a quota denial to a non-admin caller (allocations.request needs only CONTRIBUTOR)
    # must NOT name the admin-only accounting.set_quota tool; it points at the plain breadcrumb
    # and tells the caller to ask a project admin.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, quota=0)
            return await _request(pool, _ctx(Role.CONTRIBUTOR))

    resp = asyncio.run(_run())
    assert resp.error_category == "quota_exceeded"
    assert resp.suggested_next_actions == ["allocations.list"]
    assert "accounting.set_quota" not in resp.suggested_next_actions
    assert resp.detail is not None
    assert "accounting." not in resp.detail
    assert "ask your project admin" in resp.detail


def test_budget_denial_names_set_budget_remedy_for_admin(migrated_url: str) -> None:
    # #801/ADR-0245: a budget denial (the second step of the fresh-project trap, surfaced after
    # quota is raised) points at accounting.set_budget for an admin caller (#841).
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, limit="0")  # generous quota, no budget
            return await _request(pool, _ctx(Role.ADMIN))

    resp = asyncio.run(_run())
    assert resp.error_category == "allocation_denied"
    assert resp.data["reason"] == "budget_exceeded"
    assert resp.suggested_next_actions[0] == "accounting.set_budget"
    assert "allocations.list" in resp.suggested_next_actions
    assert resp.detail is not None and "accounting.set_budget" in resp.detail


def test_budget_denial_omits_admin_tool_for_non_admin(migrated_url: str) -> None:
    # #841: a budget denial to a non-admin caller must drop accounting.set_budget and tell the
    # caller to ask a project admin — while STILL naming the shortfall figure so the admin
    # can be asked for a sized increase (#833 surfaces it in data["unmet"]).
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, limit="0")
            return await _request(pool, _ctx(Role.CONTRIBUTOR))

    resp = asyncio.run(_run())
    assert resp.data["reason"] == "budget_exceeded"
    assert resp.suggested_next_actions == ["allocations.list"]
    assert "accounting.set_budget" not in resp.suggested_next_actions
    budget = _gate_entry(resp, "budget")
    required_kcu = str(budget["required_kcu"])
    assert resp.detail is not None
    assert "accounting." not in resp.detail
    assert "ask your project admin" in resp.detail
    assert required_kcu in resp.detail  # the shortfall is still named for the non-admin


def test_budget_denial_reports_cost_and_remaining(migrated_url: str) -> None:
    # #833/#838: a budget denial surfaces the budget gate's figures in data["unmet"], names the
    # shortfall in the prose, and carries the absolute required_limit_kcu, so the agent sizes
    # accounting.set_budget rather than over-setting blind.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, limit="0")  # generous quota, zero budget
            return await _request(pool, _ctx(Role.ADMIN))

    resp = asyncio.run(_run())
    assert resp.data["reason"] == "budget_exceeded"
    budget = _gate_entry(resp, "budget")
    required_kcu = str(budget["required_kcu"])
    assert Decimal(required_kcu) > 0
    assert Decimal(str(budget["required_limit_kcu"])) == Decimal(required_kcu)  # spent 0
    assert Decimal(str(budget["limit_kcu"])) == Decimal("0")
    assert Decimal(str(budget["spent_kcu"])) == Decimal("0")
    assert Decimal(str(budget["remaining_kcu"])) == Decimal("0")
    assert budget["remedy"] == "accounting.set_budget"
    assert resp.detail is not None
    assert required_kcu in resp.detail  # the prose names the shortfall
    assert "accounting.set_budget" in resp.detail


def test_both_funding_gates_unmet_aggregate_admin(migrated_url: str) -> None:
    # #833: a fresh project trips quota AND budget; one denial enumerates both gates with their
    # remedies so an admin provisions both at once. Top-level category stays the gate's primary.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, quota=0, limit="0")  # both rows present, both exhausted
            return await _request(pool, _ctx(Role.ADMIN))

    resp = asyncio.run(_run())
    assert resp.error_category == "quota_exceeded"
    gates = _unmet_gates(resp)
    assert gates == ["quota", "budget"]
    assert _gate_entry(resp, "quota")["remedy"] == "accounting.set_quota"
    assert _gate_entry(resp, "budget")["remedy"] == "accounting.set_budget"
    assert resp.suggested_next_actions == [
        "accounting.set_quota",
        "accounting.set_budget",
        "allocations.list",
    ]
    assert resp.detail is not None
    assert "accounting.set_quota" in resp.detail
    assert "accounting.set_budget" in resp.detail


def test_both_funding_gates_unmet_non_admin(migrated_url: str) -> None:
    # #833/#841: a non-admin sees both unmet gates named in data and prose, but no admin tool in
    # the breadcrumb and a "ask your project admin" remedy.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2, quota=0, limit="0")
            return await _request(pool, _ctx(Role.CONTRIBUTOR))

    resp = asyncio.run(_run())
    assert _unmet_gates(resp) == ["quota", "budget"]
    assert resp.suggested_next_actions == ["allocations.list"]
    assert resp.detail is not None
    assert "accounting." not in resp.detail
    assert "ask your project admin" in resp.detail


def test_funding_denial_detail_enumerates_both_gates() -> None:
    # #833: the prose names every unmet gate + its remedy for an admin caller.
    unmet: list[dict[str, object]] = [
        {"gate": "quota", "current": 0, "required": 1},
        {"gate": "budget", "required_kcu": "6.0000", "required_limit_kcu": "6.0000"},
    ]
    detail = _funding_denial_detail(unmet, caller_is_admin=True)
    assert "accounting.set_quota" in detail
    assert "accounting.set_budget" in detail
    assert "requested 6.0000 kcu" in detail


def test_funding_denial_detail_non_admin_drops_tools() -> None:
    # #841: a non-admin gets shortfall prose but no tool names — routed to a project admin.
    unmet: list[dict[str, object]] = [
        {
            "gate": "budget",
            "required_kcu": "6.0000",
            "required_limit_kcu": "6.0000",
            "remaining_kcu": "0",
            "limit_kcu": "0",
            "spent_kcu": "0",
        },
    ]
    detail = _funding_denial_detail(unmet, caller_is_admin=False)
    assert "requested 6.0000 kcu, 0 kcu remaining" in detail
    assert "accounting.set_budget" not in detail
    assert "ask your project admin" in detail


def test_denial_next_actions_role_awareness() -> None:
    # #833/#841: the breadcrumb leads with every unmet remedy (quota then budget) for an admin
    # caller; a non-admin keeps the plain breadcrumb.
    both = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.QUOTA_EXCEEDED,
        details={"unmet": [{"gate": "quota"}, {"gate": "budget"}]},
    )
    budget = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason=BUDGET_DENIAL_REASON,
        details={"unmet": [{"gate": "budget"}]},
    )
    quota = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.QUOTA_EXCEEDED,
        details={"unmet": [{"gate": "quota"}]},
    )
    assert _denial_next_actions(both, caller_is_admin=True) == [
        "accounting.set_quota",
        "accounting.set_budget",
        "allocations.list",
    ]
    assert _denial_next_actions(budget, caller_is_admin=True)[0] == "accounting.set_budget"
    assert _denial_next_actions(budget, caller_is_admin=False) == ["allocations.list"]
    assert _denial_next_actions(quota, caller_is_admin=True)[0] == "accounting.set_quota"
    assert _denial_next_actions(quota, caller_is_admin=False) == ["allocations.list"]


def test_quota_denial_detail_role_awareness() -> None:
    # #841: the quota denial prose names accounting.set_quota only for an admin caller.
    quota = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.QUOTA_EXCEEDED,
        details={"unmet": [{"gate": "quota", "current": 0, "required": 1}]},
    )
    admin_detail = _denial_detail(quota, caller_is_admin=True)
    non_admin_detail = _denial_detail(quota, caller_is_admin=False)
    assert "accounting.set_quota" in admin_detail
    assert "accounting." not in non_admin_detail
    assert "ask your project admin" in non_admin_detail


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

    resp = _renew_response(uid, outcome, _ctx())

    assert resp.data["window"] == "0"


def _granted_alloc(*, project: str = "proj") -> Allocation:
    """A minimal GRANTED allocation for envelope role-filter tests (#862)."""
    return Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        agent_session="s",
        project=project,
        state=AllocationState.GRANTED,
        resource_id=uuid4(),
    )


def test_envelope_role_filters_success_next_actions() -> None:
    # ADR-0261: a GRANTED envelope's breadcrumb is filtered by the caller's role on the
    # allocation's project, so a non-operator is never pointed at operator-only systems.provision.
    alloc = _granted_alloc()
    operator = envelope_for_allocation(alloc, _ctx(role=Role.OPERATOR))
    contributor = envelope_for_allocation(alloc, _ctx(role=Role.CONTRIBUTOR))
    viewer = envelope_for_allocation(alloc, _ctx(role=Role.VIEWER))
    role_less = envelope_for_allocation(alloc, _ctx(role=None))

    assert operator.suggested_next_actions == [
        "allocations.get",
        "systems.provision",
        "allocations.release",
    ]
    assert contributor.suggested_next_actions == ["allocations.get", "allocations.release"]
    assert viewer.suggested_next_actions == ["allocations.get"]
    assert role_less.suggested_next_actions == []


def test_envelope_filter_is_per_project_not_connection_union() -> None:
    # Operator on "other", only contributor on the allocation's project: systems.provision must
    # be dropped — the per-project leak the connection-scoped exposure filter cannot catch.
    alloc = _granted_alloc(project="proj")
    ctx = RequestContext(
        principal="user-1",
        agent_session="s",
        projects=("proj", "other"),
        roles={"proj": Role.CONTRIBUTOR, "other": Role.OPERATOR},
    )
    resp = envelope_for_allocation(alloc, ctx)
    assert "systems.provision" not in resp.suggested_next_actions
    assert resp.suggested_next_actions == ["allocations.get", "allocations.release"]


def test_renew_response_role_filters_success_next_actions() -> None:
    # A renew that keeps a GRANTED allocation re-emits the breadcrumb; filter it too (#862).
    alloc = _granted_alloc()
    outcome = RenewOutcome(renewed=True, allocation=alloc)
    contributor = _renew_response(alloc.id, outcome, _ctx(role=Role.CONTRIBUTOR))
    operator = _renew_response(alloc.id, outcome, _ctx(role=Role.OPERATOR))

    assert "systems.provision" not in contributor.suggested_next_actions
    assert "systems.provision" in operator.suggested_next_actions


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


def test_list_allocations_paginates_with_cursor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=5)
            for _ in range(5):
                await _request(pool, _ctx())
            first = await list_allocations(pool, _ctx(), project="proj", limit=2)
            assert first.data["truncated"] is True
            cursor = first.data["next_cursor"]
            assert isinstance(cursor, str)

            seen = [item.object_id for item in first.items]
            for _ in range(10):
                page = await list_allocations(pool, _ctx(), project="proj", limit=2, cursor=cursor)
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                next_cursor = page.data["next_cursor"]
                assert isinstance(next_cursor, str)
                cursor = next_cursor
        assert len(seen) == 5
        assert len(set(seen)) == 5

    asyncio.run(_run())


def test_list_allocations_no_truncation_at_exactly_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            await _request(pool, _ctx())
            await _request(pool, _ctx())
            resp = await list_allocations(pool, _ctx(), project="proj", limit=2)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None

    asyncio.run(_run())


def test_list_allocations_malformed_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=1)
            await _request(pool, _ctx())
            resp = await list_allocations(pool, _ctx(), project="proj", limit=2, cursor="garbage")
        assert resp.status == "error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())


def test_list_allocations_filters_by_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool, cap=2)
            granted = await _seed_alloc(pool, res, AllocationState.GRANTED)
            await _seed_alloc(pool, res, AllocationState.RELEASED)
            resp = await list_allocations(
                pool, _ctx(), project="proj", limit=50, state=AllocationState.GRANTED
            )
        assert [r.object_id for r in resp.items] == [granted]

    asyncio.run(_run())


def test_list_allocations_state_filter_no_match_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool, cap=1)
            await _seed_alloc(pool, res, AllocationState.GRANTED)
            resp = await list_allocations(
                pool, _ctx(), project="proj", limit=50, state=AllocationState.FAILED
            )
        assert resp.status == "ok"
        assert resp.items == []
        assert resp.data["truncated"] is False

    asyncio.run(_run())


def test_list_allocations_state_filter_drains_across_pages(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res = await _register(pool, cap=5)
            granted = {await _seed_alloc(pool, res, AllocationState.GRANTED) for _ in range(3)}
            await _seed_alloc(pool, res, AllocationState.RELEASED)
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):
                page = await list_allocations(
                    pool,
                    _ctx(),
                    project="proj",
                    limit=2,
                    cursor=cursor,
                    state=AllocationState.GRANTED,
                )
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                next_cursor = page.data["next_cursor"]
                assert isinstance(next_cursor, str)
                cursor = next_cursor
        assert set(seen) == granted  # only granted, every one, no duplicate
        assert len(seen) == len(granted)

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
    from kdive.domain.lifecycle import Allocation

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
    null_cause = envelope_for_allocation(_make(), _ctx())
    assert null_cause.error_category == ErrorCategory.INFRASTRUCTURE_FAILURE.value
    assert null_cause.retryable is True
    # A budget terminate -> allocation_denied, terminal.
    budget = envelope_for_allocation(_make(ErrorCategory.ALLOCATION_DENIED), _ctx())
    assert budget.error_category == ErrorCategory.ALLOCATION_DENIED.value
    assert budget.retryable is False
    # A queue timeout -> queue_timeout, retryable.
    timed_out = envelope_for_allocation(_make(ErrorCategory.QUEUE_TIMEOUT), _ctx())
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
    data = envelope_for_allocation(alloc, _ctx()).data
    assert data["resource_id"] == str(res)
    assert data["requested_kind"] == ResourceKind.LOCAL_LIBVIRT.value
    assert data["requested_vcpus"] == 4
    assert data["requested_memory_gb"] == 8
    assert data["requested_disk_gb"] == 40
    assert data["shape"] == "small"
    assert data["created_at"] == _DT.isoformat()
    assert data["requested_pcie_specs"] == []
    assert data["lease_expiry"] is None


def test_envelope_surfaces_requested_pool() -> None:
    alloc = Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        resource_id=None,
        state=AllocationState.REQUESTED,
        requested_pool="big-remote",
    )
    data = envelope_for_allocation(alloc, _ctx()).data
    assert data["requested_pool"] == "big-remote"
    assert data["requested_kind"] is None


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
    resp = envelope_for_allocation(alloc, _ctx())
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

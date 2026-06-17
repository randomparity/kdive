"""The `allocations.*` MCP tools — the Allocation admission/lifecycle surface (ADR-0023).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`request` admits against the per-host cap (core `admit`); `release` drives a granted/active
allocation to `released` under a per-allocation advisory lock with an `IllegalTransition`
backstop; `get`/`list` render an allocation through `_envelope_for_allocation`, which maps
the terminal `failed` state to a `failure` envelope (its value collides with the response
envelope's failure-status set). RBAC: `request`/`release` require `operator`; reads require
`viewer` on the owning project. Authz denials raise (ADR-0020: no authz `ErrorCategory`).
A syntactically valid but absent (or ungranted, no-leak) allocation id is `not_found`; a
malformed id stays `configuration_error` (ADR-0097).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload, ResourceById, ResourceByKind
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.services.allocation.admission.core import (
    AFFINITY_DENIAL_REASON,
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
)
from kdive.services.allocation.admission.request import (
    AdmissionRequestSpec,
    RequestAdmissionResult,
    denial_details,
    request_admission,
)
from kdive.services.allocation.release import (
    ReleaseOutcome,
    ctx_audit_writer,
    release_with_backstops,
)
from kdive.services.allocation.renew import RenewOutcome, renew

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0


def _allocation_next_actions(state: AllocationState) -> list[str]:
    """Breadcrumb for a successful allocation envelope, keyed by state (#462).

    A ``granted`` allocation's next step in the create-a-VM flow is ``systems.provision`` (it
    consumes ``allocation_id``), so it is advertised between the read and the release. A queued
    ``requested`` allocation holds no host yet, so it stays read-or-release until promoted.
    """
    if state is AllocationState.GRANTED:
        return ["allocations.get", "systems.provision", "allocations.release"]
    return ["allocations.get", "allocations.release"]


async def _queue_position(conn: AsyncConnection, alloc: Allocation) -> int:
    """1-based FIFO rank of a ``requested`` allocation among same-target queued rows.

    Same target is the by-id ``requested_resource_id`` or the by-kind ``requested_kind``,
    ordered ``(created_at, id)`` — the order ``promote_pending`` selects on. An **advisory
    hint, not an ETA**: promotion is work-conserving and per-host (ADR-0118), so a younger
    request on a free host can be promoted ahead of an older one on a busy host.
    """
    if alloc.requested_resource_id is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = 'requested' "
            "AND requested_resource_id = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        target: object = alloc.requested_resource_id
    elif alloc.requested_kind is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = 'requested' "
            "AND requested_kind = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        target = alloc.requested_kind.value
    else:
        return 1  # A requested row with no target is degenerate; report "next in line".
    async with conn.cursor() as cur:
        await cur.execute(query, {"target": target, "created_at": alloc.created_at, "id": alloc.id})
        row = await cur.fetchone()
    ahead = int(row[0]) if row is not None else 0
    return ahead + 1


def _envelope_for_allocation(
    alloc: Allocation, *, queue_position: int | None = None
) -> ToolResponse:
    """Render an allocation; ``failed`` becomes a failure envelope (ADR-0023 §6).

    A failed allocation reports its persisted ``failure_category`` (ADR-0118), falling back
    to ``infrastructure_failure`` when unset. A ``requested`` row carries the advisory
    ``queue_position``/``queue_ahead`` hint when one was computed (ADR-0118).
    """
    if alloc.state is AllocationState.FAILED:
        category = alloc.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(alloc.id),
            category,
            data={"current_status": alloc.state.value},
        )
    data: dict[str, JsonValue] = {"project": alloc.project}
    if alloc.state is AllocationState.REQUESTED and queue_position is not None:
        data["queue_position"] = queue_position
        data["queue_ahead"] = queue_position - 1
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=_allocation_next_actions(alloc.state),
        data=data,
    )


def _spec_from_payload(payload: AllocationRequestPayload) -> AdmissionRequestSpec | ToolResponse:
    resolved_id: UUID | None = None
    kind = ResourceByKind().kind
    if isinstance(payload.resource, ResourceById):
        resolved_id = _as_uuid(payload.resource.resource_id)
        if resolved_id is None:
            return _config_error(payload.resource.resource_id)
    else:
        kind = payload.resource.kind
    return AdmissionRequestSpec(
        resource_id=resolved_id,
        kind=kind,
        shape=payload.shape,
        vcpus=payload.vcpus,
        memory_gb=payload.memory_gb,
        disk_gb=payload.disk_gb,
        window=payload.window,
        pcie_devices=tuple(payload.pcie_devices),
        on_capacity=payload.on_capacity,
    )


async def request_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    request: AllocationRequestPayload,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an allocation against the project budget/quota and the selected host's cap.

    Builds the request selector, resolves the target Resource, and runs the admission
    gate (ADR-0007 §5). A grant returns the allocation id; a denial maps to the gate's
    most specific category — ``quota_exceeded`` (over the concurrency cap),
    ``allocation_denied`` (over budget or host cap), or ``configuration_error`` (a
    malformed selector/window or an over-caps size). Requires ``operator`` on ``project``.

    With ``on_capacity=queue`` a **capacity** denial (host cap or concurrency quota) instead
    enqueues a durable ``requested`` allocation holding only a queue position (ADR-0069), and
    the response reports ``requested``. Budget and configuration denials always hard-deny.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        spec = _spec_from_payload(request)
        if isinstance(spec, ToolResponse):
            return spec
        async with pool.connection() as conn:
            result = await request_admission(
                conn,
                ctx,
                project=project,
                spec=spec,
                idempotency_key=idempotency_key,
            )
        return _request_response(result)


_DISCOVERY_NEXT_ACTIONS = ["resources.list", "shapes.list"]


def _request_response(result: RequestAdmissionResult) -> ToolResponse:
    """Map service-level request admission output to the MCP response envelope."""
    if result.error is not None:
        return ToolResponse.failure_from_error(result.object_id, result.error)
    if result.resource is None:
        return _no_resource_response(result)
    if result.allocation is not None:
        return _grant_or_enqueue_response(result.resource, result.project, result.allocation)
    if result.denial is not None:
        return _denial_response(result.resource.id, result.project, result.denial)
    return ToolResponse.failure(result.object_id, ErrorCategory.INFRASTRUCTURE_FAILURE)


def _no_resource_response(result: RequestAdmissionResult) -> ToolResponse:
    """Envelope a no-schedulable-resource denial with a cause+fix detail (#471, ADR-0132).

    A by-kind denial (``available_kinds`` populated) names the selected kind and what kinds
    *are* registered; a by-id denial (``available_kinds is None``) names the caller-supplied
    id. Both point at the discovery tools so a black-box agent can recover from the envelope.
    """
    if result.available_kinds is not None:
        if result.available_kinds:
            available = f"available kinds: {', '.join(result.available_kinds)}"
        else:
            available = "no resource kinds are registered"
        detail = f"no schedulable {result.object_id!r} resource is registered; {available}"
    else:
        detail = f"no schedulable resource {result.object_id!r} is registered"
    return ToolResponse.failure(
        result.object_id,
        result.category or ErrorCategory.CONFIGURATION_ERROR,
        detail=detail,
        suggested_next_actions=list(_DISCOVERY_NEXT_ACTIONS),
    )


def _grant_or_enqueue_response(
    resource: Resource, project: str, allocation: Allocation
) -> ToolResponse:
    """Render a grant or a queued-enqueue success (ADR-0069).

    A grant carries the chosen host's ``resource_id`` and reports ``granted``; a queued
    ``requested`` allocation reports ``requested`` and carries no ``resource_id`` (it holds
    only a queue position, not a host).
    """
    data = {"project": project}
    if allocation.state is not AllocationState.REQUESTED:
        data["resource_id"] = str(resource.id)
    return ToolResponse.success(
        str(allocation.id),
        allocation.state.value,
        suggested_next_actions=_allocation_next_actions(allocation.state),
        data=data,
    )


def _denial_response(resource_id: UUID, project: str, outcome: AdmissionOutcome) -> ToolResponse:
    """Map a denial outcome to its typed failure envelope (category-specific)."""
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data = denial_details(outcome)
    _log.info("allocation denied for project %s on resource %s: %s", project, resource_id, category)
    return ToolResponse.failure(
        str(resource_id),
        category,
        detail=_denial_detail(outcome),
        suggested_next_actions=["allocations.list"],
        data=data,
    )


def _denial_detail(outcome: AdmissionOutcome) -> str:
    """Author-controlled prose for a capacity/budget/quota denial (#471, ADR-0132).

    Keyed off the internal ``outcome.reason`` token (never surfaced verbatim) so a new token
    cannot leak as ``detail``; the host-cap case adds the cap/in-use counters.
    """
    if outcome.reason == BUDGET_DENIAL_REASON:
        return "project budget exhausted for the requested window"
    if outcome.reason == AFFINITY_DENIAL_REASON:
        return "the project is not permitted to place on the selected resource"
    if outcome.category is ErrorCategory.QUOTA_EXCEEDED:
        return "project concurrency quota exhausted"
    if outcome.reason == "at_capacity":
        cap = "?" if outcome.cap is None else str(outcome.cap)
        in_use = "?" if outcome.in_use is None else str(outcome.in_use)
        return f"host capacity exhausted (cap {cap}, in use {in_use})"
    return "allocation denied"


async def get_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Return an allocation the caller's project owns, or a `not_found` error (no-leak)."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.VIEWER)
            position = (
                await _queue_position(conn, alloc)
                if alloc.state is AllocationState.REQUESTED
                else None
            )
        return _envelope_for_allocation(alloc, queue_position=position)


async def release_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Drive an allocation to ``released`` (under a per-allocation lock)."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
        outcome = await release_with_backstops(
            pool, uid, project=alloc.project, audit_writer=ctx_audit_writer(ctx)
        )
        return _release_response(uid, outcome)


def _release_response(uid: UUID, outcome: ReleaseOutcome) -> ToolResponse:
    """Map release service outcome to the allocations MCP envelope."""
    if outcome.released:
        return ToolResponse.success(str(uid), "released")
    data: dict[str, Any] = dict(outcome.details)
    if outcome.current_status:
        data["current_status"] = outcome.current_status
    category = outcome.category or ErrorCategory.CONFIGURATION_ERROR
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"]
        if category is ErrorCategory.STALE_HANDLE
        else [],
        data=data,
    )


async def renew_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    *,
    extend: object,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Extend an allocation's lease window, re-charged and re-checked (ADR-0036 §3).

    Resolves the allocation, requires ``operator`` on its project, and runs renew
    (under the ``PROJECT`` lock). A success returns the extended allocation id; a denial
    maps to the most specific category — ``configuration_error`` (``extend ≤ 0``, a bad
    id, or the lease already at ``KDIVE_LEASE_MAX``), ``stale_handle`` (a terminal
    allocation), or ``allocation_denied`` (over budget for the added window, window
    unchanged). A replayed ``idempotency_key`` returns the prior result with no second
    extend or charge.
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
            outcome = await renew(
                conn, ctx, allocation_id=uid, extend=extend, idempotency_key=idempotency_key
            )
        return _renew_response(uid, outcome)


def _renew_response(uid: UUID, outcome: RenewOutcome) -> ToolResponse:
    """Map a :class:`RenewOutcome` to its typed envelope (success or category-specific)."""
    if outcome.renewed and outcome.allocation is not None:
        return ToolResponse.success(
            str(uid),
            outcome.allocation.state.value,
            suggested_next_actions=_allocation_next_actions(outcome.allocation.state),
            data={"project": outcome.allocation.project},
        )
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data: dict[str, Any] = dict(outcome.details)
    if outcome.current_status:
        data["current_status"] = outcome.current_status
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"],
        data=data,
    )


async def wait_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    timeout_s: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ToolResponse:
    """Poll until the allocation leaves ``requested`` or ``timeout_s`` (clamped) elapses.

    A queued ``requested`` allocation settles into ``granted`` (promoted), ``released``
    (cancelled), or ``failed`` (budget terminate / ``queue_timeout`` reap). Each poll
    acquires and releases a pool connection (holds none while sleeping); a non-positive or
    non-finite timeout means a single read. Auth/no-leak match ``allocations.get`` (ADR-0118).
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    if not math.isfinite(timeout_s):
        return _config_error(allocation_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(max(timeout_s, 0.0), MAX_WAIT_S)
    with bind_context(principal=ctx.principal):
        while True:
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, uid)
                if alloc is None or alloc.project not in ctx.projects:
                    return _not_found(allocation_id)
                require_role(ctx, alloc.project, Role.VIEWER)
                position = (
                    await _queue_position(conn, alloc)
                    if alloc.state is AllocationState.REQUESTED
                    else None
                )
            now = loop.time()
            if alloc.state is not AllocationState.REQUESTED or now >= deadline:
                return _envelope_for_allocation(alloc, queue_position=position)
            await sleep(min(POLL_INTERVAL_S, deadline - now))


async def list_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit: int
) -> ToolResponse:
    """Return the newest allocations for ``project`` in one collection envelope.

    Accepting a granted ``project`` here is working-as-designed (the caller is a
    viewer+ member of it): this is the viewer floor, not a discovery hole (#426 note).
    Which projects a token grants is discoverable via ``accounting.report_granted_set``
    (#426) and ``projects.list`` (#427) — not by probing this tool.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
    capped = _clamp_list_limit(limit)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM allocations WHERE project = %s "
                "ORDER BY created_at DESC, id LIMIT %s",
                (project, capped),
            )
            rows = await cur.fetchall()
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                responses.append(_envelope_for_allocation(Allocation.model_validate(row)))
            except ValueError:
                _log.warning("allocation row violates the response invariant; degraded")
                responses.append(
                    ToolResponse.failure(
                        str(row.get("id", "?")), ErrorCategory.INFRASTRUCTURE_FAILURE
                    )
                )
        return ToolResponse.collection(
            "allocations",
            "ok",
            responses,
            suggested_next_actions=["allocations.get", "allocations.release"],
            data={"project": project},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `allocations.*` tools on ``app``, bound to ``pool``."""
    _register_allocations_request(app, pool)
    _register_allocations_get(app, pool)
    _register_allocations_release(app, pool)
    _register_allocations_renew(app, pool)
    _register_allocations_list(app, pool)
    _register_allocations_wait(app, pool)


def _register_allocations_request(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="allocations.request",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def allocations_request(
        project: Annotated[str, Field(description="Project to admit the allocation for.")],
        request: Annotated[
            AllocationRequestPayload,
            Field(description="Allocation request payload: size, lease window, resource selector."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior grant."),
        ] = None,
    ) -> ToolResponse:
        """Request capacity and create an allocation grant."""
        return await request_allocation(
            pool,
            current_context(),
            project=project,
            request=request,
            idempotency_key=idempotency_key,
        )


def _register_allocations_get(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="allocations.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def allocations_get(
        allocation_id: Annotated[str, Field(description="The Allocation to render.")],
    ) -> ToolResponse:
        """Return one allocation visible to the caller."""
        return await get_allocation(pool, current_context(), allocation_id)


def _register_allocations_release(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="allocations.release",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def allocations_release(
        allocation_id: Annotated[str, Field(description="The Allocation to release.")],
    ) -> ToolResponse:
        """Release an active allocation."""
        return await release_allocation(pool, current_context(), allocation_id)


def _register_allocations_renew(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="allocations.renew",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def allocations_renew(
        allocation_id: Annotated[str, Field(description="The Allocation to renew.")],
        extend: Annotated[
            float | str,
            Field(description="Additional hours to add (number or decimal string, > 0)."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior renewal."),
        ] = None,
    ) -> ToolResponse:
        """Extend an allocation lease window."""
        return await renew_allocation(
            pool,
            current_context(),
            allocation_id,
            extend=extend,
            idempotency_key=idempotency_key,
        )


def _register_allocations_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="allocations.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def allocations_list(
        project: Annotated[str, Field(description="Project whose allocations to list.")],
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
    ) -> ToolResponse:
        """List allocations visible in a project."""
        return await list_allocations(pool, current_context(), project=project, limit=limit)


def _register_allocations_wait(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="allocations.wait",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def allocations_wait(
        allocation_id: Annotated[
            str,
            Field(
                description=("The Allocation to poll until it leaves the requested (queued) state.")
            ),
        ],
        timeout_s: Annotated[
            float, Field(description="Maximum seconds to wait (capped at 300).")
        ] = 30.0,
    ) -> ToolResponse:
        """Poll until the allocation leaves the queued state or the deadline elapses.

        Blocks (long-poll) until the ``requested`` allocation is promoted to ``granted``,
        cancelled to ``released``, or terminated to ``failed``. Returns the settled
        envelope immediately when the allocation is already settled. ``timeout_s`` is
        capped at 300 s; a zero or negative value means a single read with no wait.
        """
        return await wait_allocation(pool, current_context(), allocation_id, timeout_s)

"""Shared allocation response rendering helpers."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.exposure import visible_next_actions
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools.lifecycle._recovery import iso
from kdive.security.authz.context import RequestContext

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0


def allocation_next_actions(state: AllocationState) -> list[str]:
    """Return the next tool breadcrumbs for an allocation state."""
    if state is AllocationState.GRANTED:
        return ["allocations.get", "systems.provision", "allocations.release"]
    return ["allocations.get", "allocations.release"]


async def queue_position(conn: AsyncConnection, alloc: Allocation) -> int:
    """Return the 1-based FIFO rank of a requested allocation for its target."""
    params: dict[str, object] = {
        "state": AllocationState.REQUESTED.value,
        "created_at": alloc.created_at,
        "id": alloc.id,
    }
    if alloc.requested_resource_id is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = %(state)s "
            "AND requested_resource_id = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        params["target"] = alloc.requested_resource_id
    elif alloc.requested_kind is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = %(state)s "
            "AND requested_kind = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        params["target"] = alloc.requested_kind.value
    elif alloc.requested_pool is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = %(state)s "
            "AND requested_pool = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        params["target"] = alloc.requested_pool
    else:
        return 1
    async with conn.cursor() as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    ahead = int(row[0]) if row is not None else 0
    return ahead + 1


def _allocation_recovery(alloc: Allocation) -> dict[str, JsonValue]:
    """Selector, sizing, placement, and timing already on the Allocation row (#568)."""
    return {
        "requested_kind": alloc.requested_kind.value if alloc.requested_kind else None,
        "requested_pool": alloc.requested_pool,
        "requested_resource_id": (
            str(alloc.requested_resource_id) if alloc.requested_resource_id else None
        ),
        "requested_pcie_specs": list(alloc.requested_pcie_specs),
        "shape": alloc.shape,
        "requested_vcpus": alloc.requested_vcpus,
        "requested_memory_gb": alloc.requested_memory_gb,
        "requested_disk_gb": alloc.requested_disk_gb,
        "resource_id": str(alloc.resource_id) if alloc.resource_id else None,
        "lease_expiry": iso(alloc.lease_expiry),
        "active_started_at": iso(alloc.active_started_at),
        "active_ended_at": iso(alloc.active_ended_at),
        "created_at": iso(alloc.created_at),
        "updated_at": iso(alloc.updated_at),
    }


def envelope_for_allocation(
    alloc: Allocation, ctx: RequestContext, *, queue_position: int | None = None
) -> ToolResponse:
    """Render an allocation as the public MCP response envelope.

    Success-envelope ``suggested_next_actions`` are role-filtered against the caller's grant on
    the allocation's project (ADR-0261), so a non-operator is never pointed at operator-only
    ``systems.provision``.
    """
    recovery = _allocation_recovery(alloc)
    if alloc.state is AllocationState.FAILED:
        category = alloc.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(alloc.id),
            category,
            data={"current_status": alloc.state.value, **recovery},
        )
    data: dict[str, JsonValue] = {"project": alloc.project, **recovery}
    if alloc.state is AllocationState.REQUESTED and queue_position is not None:
        data["queue_position"] = queue_position
        data["queue_ahead"] = queue_position - 1
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=visible_next_actions(
            allocation_next_actions(alloc.state), ctx, alloc.project
        ),
        data=data,
    )

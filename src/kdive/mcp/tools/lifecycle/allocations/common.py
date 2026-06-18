"""Shared allocation response rendering helpers."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.mcp.responses import JsonValue, ToolResponse

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0


def allocation_next_actions(state: AllocationState) -> list[str]:
    """Return the next tool breadcrumbs for an allocation state."""
    if state is AllocationState.GRANTED:
        return ["allocations.get", "systems.provision", "allocations.release"]
    return ["allocations.get", "allocations.release"]


async def queue_position(conn: AsyncConnection, alloc: Allocation) -> int:
    """Return the 1-based FIFO rank of a requested allocation for its target."""
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
        return 1
    async with conn.cursor() as cur:
        await cur.execute(query, {"target": target, "created_at": alloc.created_at, "id": alloc.id})
        row = await cur.fetchone()
    ahead = int(row[0]) if row is not None else 0
    return ahead + 1


def envelope_for_allocation(
    alloc: Allocation, *, queue_position: int | None = None
) -> ToolResponse:
    """Render an allocation as the public MCP response envelope."""
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
        suggested_next_actions=allocation_next_actions(alloc.state),
        data=data,
    )


_allocation_next_actions = allocation_next_actions
_queue_position = queue_position
_envelope_for_allocation = envelope_for_allocation

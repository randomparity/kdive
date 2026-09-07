"""Shared allocation response rendering helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg import AsyncConnection

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.exposure import visible_next_actions
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools.lifecycle._recovery import iso
from kdive.security.authz.context import RequestContext

POLL_INTERVAL_S = 0.5
"""Poll cadence for ``allocations.wait``.

Local tuning, deliberately *not* shared with ``jobs.wait``'s identically-valued constant: the
two loops poll different tables and are free to diverge. The wait default and cap are the
opposite case and live in ``tools._common`` (ADR-0476).
"""


_LEASEHOLDING_STATES = (AllocationState.GRANTED, AllocationState.ACTIVE)
"""The states whose envelope discloses a lease deadline, so whose breadcrumb names its recovery.

`renew` refuses only the terminal states (``services/allocation/renew.py``), so both of these
extend normally. The breadcrumb follows the lease rather than ``granted`` alone: an ``active``
allocation is the one whose expiry costs a provisioned System.
"""


def allocation_next_actions(state: AllocationState) -> list[str]:
    """Return the next tool breadcrumbs for an allocation state.

    A leaseholding state names ``allocations.renew`` unconditionally rather than on a proximity
    heuristic: a disclosed deadline without its recovery tool is the half-contract AGENTS.md's
    "State a limit's full contract" rules out (#1336, #2306).
    """
    actions = ["allocations.wait"]
    if state is AllocationState.GRANTED:
        actions.append("systems.provision")
    if state in _LEASEHOLDING_STATES:
        actions.append("allocations.renew")
    actions.append("allocations.release")
    return actions


def _utc_iso(when: datetime) -> str:
    """Render a tz-aware instant as ISO-8601 UTC (#1336).

    psycopg renders a ``timestamptz`` in the DB session's timezone, which is not guaranteed UTC,
    so normalize before formatting rather than trusting the session offset. The lease-deadline
    contract promises UTC.
    """
    return when.astimezone(UTC).isoformat()


async def lease_reference_clock(conn: AsyncConnection) -> str:
    """Read the database wall clock — the reference an absolute lease deadline is measured on.

    ``clock_timestamp()`` rather than ``now()`` so an open transaction cannot pin the reading to
    its start. Call sites gate this on the allocation actually holding a lease, so a queued poll
    pays nothing.
    """
    row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
    if row is None:  # pragma: no cover - a scalar SELECT always returns exactly one row
        raise RuntimeError("SELECT clock_timestamp() returned no row")
    return _utc_iso(row[0])


def lease_deadline_data(alloc: Allocation, server_time: str | None) -> dict[str, JsonValue]:
    """The lease deadline and its reference clock, or ``{}`` for an allocation holding no lease.

    The single enforcement point for the disclosure across the grant, renew, and read paths
    (#2306): an absolute deadline is never surfaced without the clock it is measured against,
    because an agent has no wall clock of its own and fills the gap with worst-case guesses.
    Mirrors the shipped ``build_expires_at`` contract in ``runs/common.py``.
    """
    if alloc.lease_expiry is None:
        return {}
    if server_time is None:
        raise ValueError("server_time is required with lease_expiry")
    return {"lease_expiry": _utc_iso(alloc.lease_expiry), "server_time": server_time}


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
    alloc: Allocation,
    ctx: RequestContext,
    *,
    queue_position: int | None = None,
    server_time: str | None = None,
) -> ToolResponse:
    """Render an allocation as the public MCP response envelope.

    Success-envelope ``suggested_next_actions`` are role-filtered against the caller's grant on
    the allocation's project (ADR-0261), so a non-operator is never pointed at operator-only
    ``systems.provision``.

    ``server_time`` is the caller-read reference clock (`lease_reference_clock`), required
    whenever the allocation holds a lease. The read handler reads it, because this renderer is
    synchronous. It is spread **after** ``recovery`` on both branches so the UTC-normalized
    ``lease_expiry`` supersedes the session-offset one ``_allocation_recovery`` renders; a
    failed allocation keeps its deadline, which is exactly where a caller has to tell a lease
    still worth renewing from one already gone.
    """
    recovery = _allocation_recovery(alloc)
    deadline = lease_deadline_data(alloc, server_time)
    if alloc.state is AllocationState.FAILED:
        category = alloc.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(alloc.id),
            category,
            data={"current_status": alloc.state.value, **recovery, **deadline},
        )
    data: dict[str, JsonValue] = {"project": alloc.project, **recovery, **deadline}
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

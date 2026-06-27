"""Registrar for the `allocations.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from opentelemetry import metrics as otel_metrics
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import AllocationState
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload, ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT
from kdive.mcp.tools.lifecycle.allocations.lifecycle import (
    release_allocation as _release_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.lifecycle import renew_allocation as _renew_allocation
from kdive.mcp.tools.lifecycle.allocations.request import (
    request_allocation as _request_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.view import get_allocation as _get_allocation
from kdive.mcp.tools.lifecycle.allocations.view import list_allocations as _list_allocations
from kdive.mcp.tools.lifecycle.allocations.view import wait_allocation as _wait_allocation
from kdive.services.allocation.admission.metrics import AdmissionMetrics


class _AllocationsListPayload(ToolPayload):
    """Public payload for ``allocations.list`` filters and pagination."""

    project: str = Field(description="Project whose allocations to list.")
    state: AllocationState | None = Field(
        default=None, description="Only allocations in this lifecycle state."
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT, description="Maximum rows returned (capped at 200)."
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
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
    # Constructed at build time off the proxy meter (ADR-0190 D), like TelemetryMiddleware: the
    # proxy binds to the real MeterProvider once init_telemetry installs it, so this needs no
    # process-global lazy singleton and no boot-order dependency from the handler.
    admission_metrics = AdmissionMetrics(meter=otel_metrics.get_meter("kdive.mcp"))

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
        return await _request_allocation(
            pool,
            current_context(),
            project=project,
            request=request,
            idempotency_key=idempotency_key,
            admission_metrics=admission_metrics,
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
        return await _get_allocation(pool, current_context(), allocation_id)


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
        return await _release_allocation(pool, current_context(), allocation_id)


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
        return await _renew_allocation(
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
        request: Annotated[
            _AllocationsListPayload,
            Field(description="Allocations list filters and pagination request."),
        ],
    ) -> ToolResponse:
        """List allocations visible in a project, newest first, filterable by state.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page. The ``state`` filter composes with the cursor.
        """
        return await _list_allocations(
            pool,
            current_context(),
            project=request.project,
            limit=request.limit,
            cursor=request.cursor,
            state=request.state,
        )


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
        """Poll until the allocation leaves the queued state or the deadline elapses."""
        return await _wait_allocation(pool, current_context(), allocation_id, timeout_s)

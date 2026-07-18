"""Registrar for the `allocations.*` MCP tools."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastmcp import FastMCP
from opentelemetry import metrics as otel_metrics
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import CategorizedError
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import (
    AllocationRequestPayload,
    ResourceByKind,
    ResourceSelector,
    ToolPayload,
)
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from kdive.mcp.tools.lifecycle.allocations.common import MAX_WAIT_S
from kdive.mcp.tools.lifecycle.allocations.lifecycle import (
    release_allocation as _release_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.lifecycle import renew_allocation as _renew_allocation
from kdive.mcp.tools.lifecycle.allocations.request import (
    _guard_resource_kind,
)
from kdive.mcp.tools.lifecycle.allocations.request import (
    request_allocation as _request_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.view import (
    AllocationsListRequest,
)
from kdive.mcp.tools.lifecycle.allocations.view import (
    get_allocation as _get_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.view import (
    list_allocations as _list_allocations,
)
from kdive.mcp.tools.lifecycle.allocations.view import (
    wait_allocation as _wait_allocation,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.services.allocation.admission.metrics import AdmissionMetrics

_DEFAULT_RESOURCE_SELECTOR: ResourceSelector = ResourceByKind()
_DEFAULT_PCIE_DEVICES: list[str] = []


class _AllocationsListPayload(ToolPayload):
    """Public payload for ``allocations.list`` filters and pagination."""

    project: str | None = Field(
        default=None,
        description="Optional project whose allocations to list; omitted lists readable projects.",
    )
    state: AllocationState | None = Field(
        default=None, description="Only allocations in this lifecycle state."
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `allocations.*` tools on ``app``, bound to ``pool``.

    Args:
        app: The FastMCP application to register tools on.
        pool: Async database connection pool.
        resolver: Provider resolver used for call-time kind guard (ADR-0269).
    """
    _register_allocations_request(app, pool, resolver)
    _register_allocations_get(app, pool)
    _register_allocations_release(app, pool)
    _register_allocations_renew(app, pool)
    _register_allocations_list(app, pool)
    _register_allocations_wait(app, pool)


def _register_allocations_request(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
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
        shape: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Named size from `shapes.list`; mutually exclusive with "
                    "vcpus/memory_gb/disk_gb (supply exactly one sizing source)."
                ),
            ),
        ] = None,
        vcpus: Annotated[
            int | None,
            Field(
                default=None,
                description=("Guest vCPUs (part of the custom triple; omit when using a shape)."),
            ),
        ] = None,
        memory_gb: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "Guest memory in GB (part of the custom triple; omit when using a shape)."
                ),
            ),
        ] = None,
        disk_gb: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "Guest disk in GB (part of the custom triple; omit when using a shape). Sizes "
                    "the guest's usable disk — the filesystem grows to fill it on first boot — so "
                    "allow headroom for tool installs + build artifacts + a vmcore. Bounded by the "
                    "host disk ceiling (over-ceiling is a configuration_error)."
                ),
            ),
        ] = None,
        window: Annotated[
            Decimal | None,
            Field(default=None, gt=0, description="Lease window length in hours, e.g. 24."),
        ] = None,
        resource: Annotated[
            ResourceSelector,
            Field(
                discriminator="mode",
                description=(
                    "Resource selector chosen by its 'mode': by kind (default), by id, or by "
                    "pool. Omit to select any resource of the default kind."
                ),
            ),
        ] = _DEFAULT_RESOURCE_SELECTOR,
        arch: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Guest architecture to place and price for (e.g. 'ppc64le'); omit for an "
                    "architecture-blind request. When set, only hosts that can boot it are "
                    "candidates (a host advertising other guest arches is skipped; one "
                    "advertising none is still eligible), and the reserved cost reflects the "
                    "host's accelerator for this arch — an emulated (TCG) guest is priced above a "
                    "native (KVM) one. The bill is finalized from the System's provisioned "
                    "architecture."
                ),
            ),
        ] = None,
        pcie_devices: Annotated[
            list[str],
            Field(
                description="PCIe match specs ('vendor:device' or 'class=NN') to resolve + claim.",
            ),
        ] = _DEFAULT_PCIE_DEVICES,
        on_capacity: Annotated[
            Literal["deny", "queue"],
            Field(
                default="deny",
                description=(
                    "On a capacity denial (host cap / concurrency quota): 'deny' (default) returns "
                    "the denial; 'queue' enqueues a durable 'requested' allocation holding a queue "
                    "position (no budget/lease/occupancy). Budget and configuration denials always "
                    "hard-deny."
                ),
            ),
        ] = "deny",
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior grant."),
        ] = None,
    ) -> ToolResponse:
        """Request capacity and create an allocation.

        Size with a named ``shape`` XOR a full custom ``{vcpus, memory_gb, disk_gb}`` triple.
        ``disk_gb`` sizes the guest's usable disk (the guest filesystem grows to fill it on
        first boot), so pick a value with headroom for runtime tool installs plus build
        artifacts plus a captured vmcore — the ``debug`` shape (`shapes.list`) is pre-sized
        for that. ``disk_gb`` is bounded by the host disk ceiling; an over-ceiling request is
        a ``configuration_error`` naming the ceiling.

        Two outcomes on success: the allocation is admitted immediately (state
        ``granted``, ready to use), or — when a capacity denial is hit with
        ``on_capacity="queue"`` — it comes back queued (state ``requested``)
        holding a queue position instead of a live grant. A queued allocation is not
        usable yet; poll `allocations.wait` on its id until it leaves the ``requested``
        state before treating it as granted.
        """
        request = AllocationRequestPayload(
            shape=shape,
            vcpus=vcpus,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            window=window,
            resource=resource,
            arch=arch,
            pcie_devices=pcie_devices,
            on_capacity=on_capacity,
        )
        try:
            _guard_resource_kind(request, resolver)  # ADR-0269: on the shared handler path
        except CategorizedError as exc:
            return ToolResponse.failure_from_error("allocations.request", exc)
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
        """Release an allocation when done.

        Idempotent on an already-released grant: releasing one returns `ok` (a no-op), so this
        is safe to call as a final cleanup step. A completed `systems.teardown` does not itself
        release the allocation, but the reconciler auto-releases the now-orphaned grant after a
        short grace, so a release call after teardown may find it already released and return
        `ok`. An `expired` (lease lapsed) or `failed` (provision failed) grant instead returns
        `stale_handle`; read `allocations.get` to see its real state.
        """
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
            _AllocationsListPayload | None,
            Field(description="Allocations list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List allocations visible to the caller, newest first, filterable by project and state.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page. The ``state`` filter composes with the cursor.
        """
        request = request or _AllocationsListPayload()
        return await _list_allocations(
            pool,
            current_context(),
            AllocationsListRequest(
                project=request.project,
                limit=request.limit,
                cursor=request.cursor,
                state=request.state,
            ),
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
            float,
            Field(
                description=(
                    f"Seconds to wait before returning; capped at {int(MAX_WAIT_S)}. A "
                    "non-terminal return is the 'still queued, call allocations.wait again' "
                    "signal; prefer repeated short waits over one long hold that an "
                    "intermediary proxy may sever."
                )
            ),
        ] = 30.0,
    ) -> ToolResponse:
        """Poll until the allocation leaves the queued state or the deadline elapses."""
        return await _wait_allocation(pool, current_context(), allocation_id, timeout_s)

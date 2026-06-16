"""Service facade for allocation request admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.cost import Selector
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.pcie import parse_match_spec
from kdive.domain.shapes import ResolvedSizing
from kdive.security.authz.context import RequestContext
from kdive.services.allocation.admission.core import (
    AdmissionOutcome,
    AllocationRequest,
    admit,
)
from kdive.services.allocation.admission.placement import (
    PlacementRequest,
    resolve_placement_candidates,
)
from kdive.services.allocation.admission.sizing import resolve_request_sizing


@dataclass(frozen=True, slots=True)
class AdmissionRequestSpec:
    """Parsed allocation request inputs before sizing, placement, and admission."""

    resource_id: UUID | None
    kind: ResourceKind
    shape: str | None
    vcpus: int | None
    memory_gb: int | None
    disk_gb: int | None
    window: object | None
    pcie_devices: tuple[str, ...]
    on_capacity: Literal["deny", "queue"]


@dataclass(frozen=True, slots=True)
class RequestAdmissionResult:
    """Service-level allocation request outcome, ready for transport rendering."""

    object_id: str
    project: str
    resource: Resource | None = None
    allocation: Allocation | None = None
    denial: AdmissionOutcome | None = None
    error: CategorizedError | None = None
    category: ErrorCategory | None = None
    # The fleet's distinct registered resource kinds, populated only on a **by-kind**
    # no-resource denial so the transport can name what *is* available (#471, ADR-0132).
    # ``None`` on a by-id denial (the caller named a host; the kind list is irrelevant).
    available_kinds: tuple[str, ...] | None = None


async def request_admission(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    project: str,
    spec: AdmissionRequestSpec,
    idempotency_key: str | None = None,
) -> RequestAdmissionResult:
    """Resolve sizing + placement and run the shared admission gate."""
    object_id = str(spec.resource_id) if spec.resource_id is not None else spec.kind.value
    try:
        sizing = await resolve_request_sizing(
            conn,
            shape=spec.shape,
            vcpus=spec.vcpus,
            memory_gb=spec.memory_gb,
            disk_gb=spec.disk_gb,
        )
        pcie_specs = _compose_pcie_specs(spec, sizing)
    except CategorizedError as exc:
        return RequestAdmissionResult(object_id, project, error=exc)

    resource = await _select_target(conn, spec.resource_id, spec.kind, pcie_specs, project)
    if resource is None:
        # A by-kind denial enumerates the available kinds for the transport detail (#471); a
        # by-id denial leaves it None (the caller named a host, so the kind list adds nothing).
        available_kinds = None if spec.resource_id is not None else await _registered_kinds(conn)
        return RequestAdmissionResult(
            object_id,
            project,
            category=ErrorCategory.CONFIGURATION_ERROR,
            available_kinds=available_kinds,
        )
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=ctx,
            resource=resource,
            project=project,
            selector=Selector(vcpus=sizing.vcpus, memory_gb=sizing.memory_gb),
            window=spec.window,
            idempotency_key=idempotency_key,
            disk_gb=sizing.disk_gb,
            shape=sizing.shape,
            pcie_specs=pcie_specs,
            on_capacity=spec.on_capacity,
            requested_kind=None if spec.resource_id is not None else spec.kind,
            requested_resource_id=spec.resource_id,
        ),
    )
    if outcome.granted and outcome.allocation is not None:
        return RequestAdmissionResult(
            object_id, project, resource=resource, allocation=outcome.allocation
        )
    return RequestAdmissionResult(object_id, project, resource=resource, denial=outcome)


def _compose_pcie_specs(spec: AdmissionRequestSpec, sizing: ResolvedSizing) -> tuple[str, ...]:
    """Compose and grammar-check explicit + shape-derived PCIe specs."""
    specs = spec.pcie_devices
    if sizing.pcie_match is not None:
        specs = (*specs, sizing.pcie_match)
    for pcie_spec in specs:
        parse_match_spec(pcie_spec)
    return specs


async def _registered_kinds(conn: AsyncConnection) -> tuple[str, ...]:
    """The fleet's distinct registered resource kinds, sorted (#471, ADR-0132).

    Deployment topology, not project-scoped data — the same aggregate ``resources.list``
    surfaces — so it is safe to name in a denial detail (no per-project existence leak).
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT kind FROM resources ORDER BY kind")
        rows = await cur.fetchall()
    return tuple(str(row[0]) for row in rows)


async def _select_target(
    conn: AsyncConnection,
    resource_id: UUID | None,
    kind: ResourceKind,
    specs: tuple[str, ...],
    project: str,
) -> Resource | None:
    """Resolve the first schedulable target, affinity- and PCIe-aware when specs are present."""
    candidates = await resolve_placement_candidates(
        conn,
        PlacementRequest(resource_id=resource_id, kind=kind, pcie_specs=specs, project=project),
    )
    if candidates.resources:
        return candidates.resources[0]
    return candidates.capacity_candidate


def denial_details(outcome: AdmissionOutcome) -> dict[str, Any]:
    """Render admission denial details without transport-specific envelope decisions."""
    data: dict[str, Any] = dict(outcome.details)
    if outcome.reason is not None:
        data["reason"] = outcome.reason
    if outcome.cap is not None:
        data["cap"] = str(outcome.cap)
    if outcome.in_use is not None:
        data["in_use"] = str(outcome.in_use)
    return data

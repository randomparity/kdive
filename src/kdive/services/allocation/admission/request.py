"""Service facade for allocation request admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.accounting.cost import Selector
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.domain.lifecycle.shapes import ResolvedSizing
from kdive.domain.pcie import parse_match_spec
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
    """Parsed allocation request inputs before sizing, placement, and admission.

    Exactly one of ``resource_id`` / ``pool`` / ``kind`` is the target selector (ADR-0186); the
    payload's discriminated union guarantees that, so at most one is non-``None`` here.
    """

    resource_id: UUID | None
    kind: ResourceKind | None
    pool: str | None
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
    # ``None`` on a by-id / by-pool denial. For a by-pool denial the available pools are
    # deliberately NOT enumerated: pool names are operator-chosen labels on affinity-scoped
    # resources, so a fleet-wide list would leak another project's private pool names (ADR-0186).
    available_kinds: tuple[str, ...] | None = None
    selector: Literal["id", "kind", "pool"] = "kind"


async def request_admission(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    project: str,
    spec: AdmissionRequestSpec,
    idempotency_key: str | None = None,
) -> RequestAdmissionResult:
    """Resolve sizing + placement and run the shared admission gate."""
    selector, object_id = _selector_and_object_id(spec)
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
        return RequestAdmissionResult(object_id, project, selector=selector, error=exc)

    resource = await _select_target(conn, spec, pcie_specs, project)
    if resource is None:
        # A by-kind denial enumerates the available kinds for the transport detail (#471); a
        # by-id denial names a specific host, and a by-pool denial must NOT enumerate pools
        # (operator-chosen labels on affinity-scoped resources — a fleet list would leak another
        # project's private pool names, ADR-0186). So available_kinds is set only for by-kind.
        available_kinds = await _registered_kinds(conn) if selector == "kind" else None
        return RequestAdmissionResult(
            object_id,
            project,
            selector=selector,
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
            requested_kind=spec.kind if selector == "kind" else None,
            requested_resource_id=spec.resource_id,
            requested_pool=spec.pool if selector == "pool" else None,
        ),
    )
    if outcome.granted and outcome.allocation is not None:
        return RequestAdmissionResult(
            object_id, project, selector=selector, resource=resource, allocation=outcome.allocation
        )
    return RequestAdmissionResult(
        object_id, project, selector=selector, resource=resource, denial=outcome
    )


def _selector_and_object_id(
    spec: AdmissionRequestSpec,
) -> tuple[Literal["id", "kind", "pool"], str]:
    """Classify the request's single target selector and its display object id (ADR-0186)."""
    if spec.resource_id is not None:
        return "id", str(spec.resource_id)
    if spec.pool is not None:
        return "pool", spec.pool
    if spec.kind is not None:
        return "kind", spec.kind.value
    return "kind", "<unspecified>"


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
    spec: AdmissionRequestSpec,
    specs: tuple[str, ...],
    project: str,
) -> Resource | None:
    """Resolve the first schedulable target, affinity- and PCIe-aware when specs are present."""
    candidates = await resolve_placement_candidates(
        conn,
        PlacementRequest(
            resource_id=spec.resource_id,
            kind=spec.kind,
            pool=spec.pool,
            pcie_specs=specs,
            project=project,
        ),
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

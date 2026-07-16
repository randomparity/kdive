"""Allocation placement candidate resolution shared by request and promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

import kdive.services.allocation.admission.pcie_claim as pcie_claim
from kdive.db.repositories import RESOURCES
from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.pcie import MatchOutcome
from kdive.services.allocation.admission.affinity import project_may_place, resource_supports_arch


@dataclass(frozen=True, slots=True)
class PlacementRequest:
    resource_id: UUID | None
    kind: ResourceKind | None = None
    pool: str | None = None
    pcie_specs: tuple[str, ...] = ()
    project: str | None = None
    arch: str | None = None


@dataclass(frozen=True, slots=True)
class PlacementCandidates:
    resources: list[Resource]
    capacity_candidate: Resource | None = None


async def resolve_placement_candidates(
    conn: AsyncConnection, request: PlacementRequest
) -> PlacementCandidates:
    """Return schedulable placement candidates, filtered by affinity and free PCIe matches.

    Candidates are first filtered by the per-project affinity predicate (a disallowed scoped
    resource is never selected, so an any-available request falls through to a legal global
    one — ADR-0112, Task 4.2). When ``request.project`` is ``None`` no affinity filtering is
    applied. The PCIe-spec filtering then narrows the affinity-allowed set.
    """
    candidates = await _schedulable_candidates(
        conn, request.resource_id, request.kind, request.pool, request.project, request.arch
    )
    if not request.pcie_specs:
        return PlacementCandidates(resources=candidates)

    resources: list[Resource] = []
    capacity_candidate: Resource | None = None
    specs = list(request.pcie_specs)
    for candidate in candidates:
        descriptors = pcie_claim.descriptors_for(candidate)
        claims = await pcie_claim.active_claims(conn, candidate.id)
        resolution = pcie_claim.resolve_union(specs, descriptors, claims=claims)
        if resolution.outcome is MatchOutcome.MATCHED:
            resources.append(candidate)
        elif resolution.outcome is MatchOutcome.CAPACITY and capacity_candidate is None:
            capacity_candidate = candidate
    return PlacementCandidates(resources=resources, capacity_candidate=capacity_candidate)


async def _schedulable_candidates(
    conn: AsyncConnection,
    resource_id: UUID | None,
    kind: ResourceKind | None,
    pool: str | None,
    project: str | None,
    arch: str | None,
) -> list[Resource]:
    """Return schedulable candidates for an explicit host, a pool, or a resource kind.

    Candidates are filtered by the per-project affinity predicate when ``project`` is set;
    a disallowed scoped resource is excluded so it is never selected (Task 4.2). An explicit
    ``resource_id`` targeting a disallowed scoped host yields no candidate. A by-pool request
    (ADR-0186) selects every schedulable resource carrying the pool label, oldest-first, exactly
    like by-kind — selection routes around a busy/cordoned member. When ``arch`` is set the
    architecture predicate additionally excludes a host that advertises guest arches without it
    (ADR-0362), so a ``ppc64le`` request routes to a ``ppc64le``-capable host.
    """
    if resource_id is not None:
        resource = await RESOURCES.get(conn, resource_id)
        if resource is None or resource.cordoned or resource.status is not ResourceStatus.AVAILABLE:
            return []
        return [resource] if _placeable(resource, project, arch) else []
    if pool is not None:
        return await _label_candidates(conn, "pool", pool, project, arch)
    if kind is not None:
        return await _label_candidates(conn, "kind", kind.value, project, arch)
    return []


async def _label_candidates(
    conn: AsyncConnection, column: LiteralString, value: str, project: str | None, arch: str | None
) -> list[Resource]:
    """Schedulable resources matching ``column = value``, oldest-first, affinity+arch-filtered."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            sql.SQL(
                "SELECT * FROM resources WHERE {col} = %s AND status = 'available' "
                "AND NOT cordoned ORDER BY created_at, id"
            ).format(col=sql.Identifier(column)),
            (value,),
        )
        rows = await cur.fetchall()
    candidates = [Resource.model_validate(row) for row in rows]
    return [candidate for candidate in candidates if _placeable(candidate, project, arch)]


def _placeable(resource: Resource, project: str | None, arch: str | None) -> bool:
    """Apply the affinity + architecture predicates; a ``None`` value disables that filter."""
    if project is not None and not project_may_place(resource, project):
        return False
    return arch is None or resource_supports_arch(resource, arch)

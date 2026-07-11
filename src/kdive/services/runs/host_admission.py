"""Shared System-host admission helpers for run creation and binding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.lifecycle.system_reuse import (
    ReuseRequirement,
    read_system_sizing,
    snapshot_satisfies,
)
from kdive.services.runs.states import (
    ALLOC_HOSTABLE,
    RUN_HOSTABLE,
    RUN_NON_TERMINAL,
    SYSTEM_GONE,
)


class RunCreateError(CategorizedError):
    """Transport-neutral run admission failure with the response object id preserved."""

    def __init__(
        self,
        object_id: str,
        message: str,
        *,
        category: ErrorCategory,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, category=category, details=details)
        self.object_id = object_id


@dataclass(frozen=True, slots=True)
class RunHostTargets:
    """The object ids needed to lock and validate a System-hosted Run."""

    investigation_id: UUID
    system_id: UUID
    allocation_id: UUID


def parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise_config_error(value)


def raise_from_categorized_error(object_id: str, exc: CategorizedError) -> NoReturn:
    raise categorized_failure(object_id, exc) from exc


def raise_config_error(
    object_id: str,
    *,
    detail: str = "invalid run creation request",
    data: dict[str, object] | None = None,
) -> NoReturn:
    raise config_failure(object_id, detail=detail, data=data)


def raise_stale_target(object_id: str, *, current_status: str) -> NoReturn:
    raise stale_failure(object_id, current_status=current_status)


def config_failure(
    object_id: str,
    *,
    detail: str = "invalid run creation request",
    data: dict[str, object] | None = None,
) -> RunCreateError:
    return RunCreateError(
        object_id,
        detail,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details=data,
    )


def stale_failure(object_id: str, *, current_status: str) -> RunCreateError:
    return RunCreateError(
        object_id,
        "stale run creation target",
        category=ErrorCategory.STALE_HANDLE,
        details={"current_status": current_status},
    )


def categorized_failure(object_id: str, exc: CategorizedError) -> RunCreateError:
    return RunCreateError(
        object_id,
        str(exc),
        category=exc.category,
        details=dict(exc.details),
    )


async def resource_kind_for_system(conn: AsyncConnection, system_id: UUID) -> ResourceKind:
    """Return the resource kind backing a System."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT r.kind FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "WHERE s.id = %s",
            (system_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"resource kind lookup found no row for system {system_id}")
    return ResourceKind(row[0])


async def check_host_preconditions(
    conn: AsyncConnection,
    targets: RunHostTargets,
    *,
    project: str,
) -> tuple[RunCreateError, None] | tuple[None, tuple[System, Allocation]]:
    """Re-check host preconditions under the held locks."""
    system = await SYSTEMS.get(conn, targets.system_id)
    blocked = _system_block_error(system, targets.system_id)
    if blocked is not None or system is None:
        return blocked or config_failure(str(targets.system_id)), None
    alloc = await ALLOCATIONS.get(conn, targets.allocation_id)
    blocked = _allocation_block_error(alloc, targets.system_id)
    if blocked is not None or alloc is None:
        return blocked or stale_failure(str(targets.system_id), current_status="missing"), None
    if system.project != project:
        return config_failure(str(targets.system_id)), None
    if await _count_non_terminal_runs(conn, targets.system_id) > 0:
        return (
            RunCreateError(
                str(targets.system_id),
                "system already has a live run",
                category=ErrorCategory.TRANSPORT_CONFLICT,
                details={"reason": "system_has_live_run"},
            ),
            None,
        )
    return None, (system, alloc)


def check_reuse_assertion(
    system: System, alloc: Allocation, requirement: ReuseRequirement
) -> RunCreateError | None:
    """Apply the optional snapshot and PCIe reuse assertion."""
    if requirement.is_empty():
        return None
    sizing = read_system_sizing(alloc, system)
    try:
        satisfied = snapshot_satisfies(sizing, alloc.pcie_claim, requirement)
    except CategorizedError as exc:
        return categorized_failure(str(system.id), exc)
    if not satisfied:
        return config_failure(str(system.id), data={"reason": "reuse_requirement_unmet"})
    return None


async def _count_non_terminal_runs(conn: AsyncConnection, system_id: UUID) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM runs WHERE system_id = %s AND state = ANY(%s)",
            (system_id, [s.value for s in RUN_NON_TERMINAL]),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


def _system_block_error(system: System | None, system_id: UUID) -> RunCreateError | None:
    if system is None:
        return config_failure(str(system_id))
    if system.state in SYSTEM_GONE:
        return stale_failure(str(system_id), current_status=system.state.value)
    if system.state not in RUN_HOSTABLE:
        return config_failure(str(system_id), data={"current_status": system.state.value})
    return None


def _allocation_block_error(alloc: Allocation | None, system_id: UUID) -> RunCreateError | None:
    if alloc is None:
        return stale_failure(str(system_id), current_status="missing")
    if alloc.state not in ALLOC_HOSTABLE:
        return stale_failure(str(system_id), current_status=alloc.state.value)
    if alloc.lease_expiry is not None and alloc.lease_expiry < datetime.now(UTC):
        return stale_failure(str(system_id), current_status="lease_expired")
    return None

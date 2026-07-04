"""The v1 report sections (ADR-0212).

Each section gathers from existing tables; ``costs`` reuses the accounting ledger
rollup rather than re-deriving its SQL. Every section selects ``cap + 1`` rows and
reports ``truncated`` when the cap is exceeded, so a clipped section is never mistaken
for a complete one. Time-sensitive sections compare against the report's shared
``as_of`` so all sections observe one instant.
"""

from __future__ import annotations

from datetime import datetime
from typing import LiteralString

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.services.accounting import ledger as accounting_ledger
from kdive.services.reports.core import (
    ReportScope,
    ReportSection,
    Row,
    SectionRows,
    Window,
)

_ACTIVE_LEASE_STATES = ("granted", "active")


def _capped(rows: list[Row], cap: int) -> SectionRows:
    """Trim ``rows`` to ``cap``, flagging truncation when a ``cap + 1`` row was fetched."""
    truncated = len(rows) > cap
    return SectionRows(rows=tuple(rows[:cap]), truncated=truncated)


def _window_clause(window: Window, column: LiteralString, params: list[object]) -> LiteralString:
    """Append half-open ``column`` bounds for ``window`` and return the SQL fragment."""
    if not window:
        return ""
    start, end = window
    clause: LiteralString = ""
    if start is not None:
        clause += " AND " + column + " >= %s"
        params.append(start)
    if end is not None:
        clause += " AND " + column + " < %s"
        params.append(end)
    return clause


async def _fetch(
    conn: AsyncConnection, sql: LiteralString, params: list[object], cap: int
) -> SectionRows:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = [dict(record) for record in await cur.fetchall()]
    return _capped(rows, cap)


class InventorySection:
    """Systems in scope with their declared size from the shape catalog."""

    key: str = "inventory"
    columns: tuple[str, ...] = (
        "system_id",
        "name",
        "project",
        "state",
        "resource_kind",
        "vcpus",
        "memory_mb",
        "disk_gb",
    )

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        params: list[object] = []
        scope_clause: LiteralString = ""
        if not scope.all_projects:
            scope_clause = " WHERE s.project = ANY(%s)"
            params.append(list(scope.projects))
        params.append(cap + 1)
        # The at-grant stamped size (requested_*) is authoritative for every System — custom or
        # shaped (ADR-0312). The shape catalog is a COALESCE fallback only for legacy allocations
        # whose requested_* predates those snapshot columns (0002/0015); a custom System (stamped,
        # no shape) now reports its real size instead of NULL. Column names/units are unchanged.
        sql: LiteralString = (
            "SELECT s.id AS system_id, s.domain_name AS name, s.project, s.state, "
            "r.kind AS resource_kind, "
            "COALESCE(a.requested_vcpus, sh.vcpus) AS vcpus, "
            "COALESCE(a.requested_memory_gb * 1024, sh.memory_mb) AS memory_mb, "
            "COALESCE(a.requested_disk_gb, sh.disk_gb) AS disk_gb "
            "FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "LEFT JOIN system_shapes sh ON sh.name = s.shape"
            + scope_clause
            + " ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
        )
        return await _fetch(conn, sql, params, cap)


class LeasesSection:
    """Allocations that are current or expired, labelled active vs stale against ``as_of``."""

    key: str = "leases"
    columns: tuple[str, ...] = (
        "allocation_id",
        "project",
        "principal",
        "state",
        "lease_expiry",
        "status",
    )

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        params: list[object] = [list(_ACTIVE_LEASE_STATES), as_of, list(_ACTIVE_LEASE_STATES)]
        scope_clause: LiteralString = ""
        if not scope.all_projects:
            scope_clause = " AND project = ANY(%s)"
            params.append(list(scope.projects))
        params.append(cap + 1)
        sql: LiteralString = (
            "SELECT id AS allocation_id, project, principal, state, lease_expiry, "
            "CASE WHEN state = ANY(%s) AND lease_expiry IS NOT NULL AND lease_expiry > %s "
            "THEN 'active' ELSE 'stale' END AS status "
            "FROM allocations "
            "WHERE (state = ANY(%s) OR state = 'expired')"
            + scope_clause
            + " ORDER BY lease_expiry DESC NULLS LAST, id DESC LIMIT %s"
        )
        return await _fetch(conn, sql, params, cap)


class ImagesSection:
    """Guest filesystem images visible to the scope (public or owned-private)."""

    key: str = "images"
    columns: tuple[str, ...] = (
        "provider",
        "name",
        "arch",
        "format",
        "visibility",
        "owner",
        "state",
    )

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        params: list[object] = []
        visibility_clause: LiteralString = "TRUE"
        if not scope.all_projects:
            visibility_clause = (
                "(visibility = 'public' OR (visibility = 'private' AND owner = ANY(%s)))"
            )
            params.append(list(scope.projects))
        params.append(cap + 1)
        sql: LiteralString = (
            "SELECT provider, name, arch, format, visibility, owner, state "
            "FROM image_catalog WHERE "
            + visibility_clause
            + " ORDER BY provider, name, arch LIMIT %s"
        )
        return await _fetch(conn, sql, params, cap)


class ActivitySection:
    """Runs created within the window (v1 activity is runs only)."""

    key: str = "activity"
    columns: tuple[str, ...] = ("run_id", "project", "system_id", "state", "created_at")

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        effective = _effective_window(window, as_of)
        params: list[object] = []
        scope_clause: LiteralString = ""
        if not scope.all_projects:
            scope_clause = " AND project = ANY(%s)"
            params.append(list(scope.projects))
        window_clause = _window_clause(effective, "created_at", params)
        params.append(cap + 1)
        sql: LiteralString = (
            "SELECT id AS run_id, project, system_id, state, created_at "
            "FROM runs WHERE TRUE"
            + scope_clause
            + window_clause
            + " ORDER BY created_at DESC, id DESC LIMIT %s"
        )
        return await _fetch(conn, sql, params, cap)


class CostsSection:
    """Incurred spend per (project, principal), reusing the accounting ledger rollup."""

    key: str = "costs"
    columns: tuple[str, ...] = ("project", "principal", "reserved", "reconciled", "variance")

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        report = await accounting_ledger.report(
            conn, projects=list(scope.projects), group_by="principal", window=window
        )
        rows: list[Row] = [
            {
                "project": row.project,
                "principal": row.principal or "",
                "reserved": str(row.reserved),
                "reconciled": str(row.reconciled),
                "variance": str(row.variance),
            }
            for row in report.rows
        ]
        return _capped(rows, cap)


def _effective_window(window: Window, as_of: datetime) -> Window:
    """Default the window's end bound to ``as_of`` for point-in-time consistency."""
    if window is None:
        return (None, as_of)
    start, end = window
    if end is None:
        return (start, as_of)
    return window


def registry() -> tuple[ReportSection, ...]:
    """Return the ordered v1 section registry."""
    return (
        InventorySection(),
        LeasesSection(),
        ImagesSection(),
        ActivitySection(),
        CostsSection(),
    )

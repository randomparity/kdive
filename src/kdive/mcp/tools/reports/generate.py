"""The reports.generate_* MCP tools (ADR-0212).

Two tools mirror the accounting reporting split: ``generate_granted_set`` (the caller's
granted projects, ``viewer`` floor) and ``generate_all_projects`` (``platform_auditor``).
Each captures one ``as_of`` snapshot, gathers the section registry, redacts free text,
returns the report inline within a per-section + byte budget, and writes the CSV/XLSX
spreadsheets to the object store (presigned URLs in ``refs``). A store outage degrades to
inline-only rather than failing the read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS, REPORT_INLINE_MAX_BYTES
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.mcp.tools._time_window import parse_timestamptz_window
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    require_platform_role,
    require_role,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue
from kdive.services.reports.artifacts import ReportArtifactStore, write_report_artifacts
from kdive.services.reports.core import Report, ReportScope, Row, Section, generate_report
from kdive.services.reports.sections import registry
from kdive.store.objectstore import object_store_from_env

_REPORT_OBJECT_ID = "report"
_GRANTED_TOOL = "reports.generate_granted_set"
_ALL_PROJECTS_TOOL = "reports.generate_all_projects"
_GRANTED_SCOPE = "granted-set"
_VALID_FORMATS = ("csv", "xlsx")

StoreFactory = Callable[[], ReportArtifactStore]


def _parse_window(window: object) -> tuple[datetime | None, datetime | None] | None:
    return parse_timestamptz_window(window, timestamp_column="created_at")


def _parse_formats(formats: list[str] | None) -> tuple[str, ...]:
    chosen = tuple(formats) if formats is not None else _VALID_FORMATS
    invalid = [fmt for fmt in chosen if fmt not in _VALID_FORMATS]
    if not chosen or invalid:
        raise CategorizedError(
            f"formats must be a non-empty subset of {list(_VALID_FORMATS)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "formats", "value": list(chosen)},
        )
    return chosen


def _resolve_granted_targets(ctx: RequestContext, named: list[str] | None) -> list[str]:
    """Return the authorized projects: default member-with-role set, or each named checked."""
    if named is None:
        return [p for p in ctx.projects if ctx.roles.get(p) is not None]
    for project in named:
        require_role(ctx, project, Role.VIEWER)
    return list(named)


async def _now(conn: AsyncConnection) -> datetime:
    async with conn.cursor() as cur:
        await cur.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:  # SELECT now() always yields one row.
        raise RuntimeError("SELECT now() returned no row")
    return row[0]


async def _all_projects_universe(conn: AsyncConnection) -> list[str]:
    """Every project the report spans: union of ledger, budgets, systems, and allocations."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT project FROM ledger UNION SELECT project FROM budgets "
            "UNION SELECT project FROM systems UNION SELECT project FROM allocations "
            "ORDER BY project"
        )
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]


def _normalize_value(value: object, redactor: Redactor) -> JsonValue:
    if value is None or isinstance(value, bool | int | float):
        return value
    return redactor.redact_text(str(value))


def _normalized_report(report: Report, redactor: Redactor) -> Report:
    """Stringify non-primitive cells and route free text through the redactor."""
    sections: list[Section] = []
    for section in report.sections:
        rows: tuple[Row, ...] = tuple(
            {key: _normalize_value(value, redactor) for key, value in row.items()}
            for row in section.rows
        )
        sections.append(
            Section(
                key=section.key,
                columns=section.columns,
                rows=rows,
                truncated=section.truncated,
            )
        )
    return Report(sections=tuple(sections), as_of=report.as_of)


def _fit_preview(rows: tuple[Row, ...], budget: int) -> tuple[list[Row], bool]:
    """Take the longest row prefix whose JSON fits ``budget``; flag if any row was dropped."""
    preview: list[Row] = []
    used = 0
    for row in rows:
        used += len(json.dumps(row))
        if used > budget:
            return preview, True
        preview.append(row)
    return preview, False


def _section_item(section: Section, budget: int) -> tuple[ToolResponse, int]:
    preview, inline_truncated = _fit_preview(section.rows, budget)
    data: dict[str, JsonValue] = {
        "section": section.key,
        "count": len(section.rows),
        "truncated": section.truncated,
        "inline_truncated": inline_truncated,
        "rows_json": json.dumps(preview),
    }
    item = ToolResponse.success(section.key, "ok", data=data)
    return item, budget - len(data["rows_json"])


def _inline_items(report: Report, budget: int) -> list[ToolResponse]:
    items: list[ToolResponse] = []
    remaining = budget
    for section in report.sections:
        item, remaining = _section_item(section, max(remaining, 0))
        items.append(item)
    return items


def _window_data(window: tuple[datetime | None, datetime | None] | None) -> JsonValue:
    if window is None:
        return ""
    start, end = window
    return [bound.isoformat() if bound else "" for bound in (start, end)]


async def _spreadsheet_refs(
    conn: AsyncConnection,
    report: Report,
    formats: tuple[str, ...],
    store_factory: StoreFactory,
    report_id: UUID,
) -> tuple[dict[str, str], dict[str, JsonValue]]:
    """Write the spreadsheets and return ``(refs, extra_data)``; degrade on store outage."""
    try:
        store = store_factory()
        refs = await write_report_artifacts(
            conn,
            report,
            formats,
            store=store,
            report_id=report_id,
            ttl=config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS),
        )
    except CategorizedError as exc:
        if exc.category is ErrorCategory.MISSING_DEPENDENCY:
            raise
        return {}, {"spreadsheet_unavailable": "store_error"}
    return refs, {}


async def _build_report(
    conn: AsyncConnection,
    scope: ReportScope,
    window: tuple[datetime | None, datetime | None] | None,
    formats: tuple[str, ...],
    *,
    secret_registry: SecretRegistry,
    store_factory: StoreFactory,
    scope_label: str,
    next_tool: str,
) -> ToolResponse:
    as_of = await _now(conn)
    report = _normalized_report(
        await generate_report(conn, scope, window, as_of, sections=registry()),
        Redactor(registry=secret_registry),
    )
    items = _inline_items(report, config.require(REPORT_INLINE_MAX_BYTES))
    report_id = uuid4()
    refs, extra = await _spreadsheet_refs(conn, report, formats, store_factory, report_id)
    data: dict[str, JsonValue] = {
        "scope": scope_label,
        "window": _window_data(window),
        "as_of": as_of.isoformat(),
        "formats": list(formats),
        "section_count": len(report.sections),
        "report_id": str(report_id),
        **extra,
    }
    return ToolResponse.collection(
        _REPORT_OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=[next_tool],
        refs=refs,
        data=data,
    )


def _report_args(
    scope: str, window: tuple[datetime | None, datetime | None] | None, formats: tuple[str, ...]
) -> dict[str, object]:
    return {"scope": scope, "window": _window_data(window), "formats": list(formats)}


async def generate_granted_set(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    secret_registry: SecretRegistry,
    projects: list[str] | None = None,
    window: object = None,
    formats: list[str] | None = None,
    store_factory: StoreFactory = object_store_from_env,
) -> ToolResponse:
    """Generate a report over the caller's granted projects (``viewer`` floor)."""
    with bind_context(principal=ctx.principal):
        try:
            parsed_window = _parse_window(window)
            parsed_formats = _parse_formats(formats)
            targets = _resolve_granted_targets(ctx, projects)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _REPORT_OBJECT_ID, exc, suggested_next_actions=[_GRANTED_TOOL]
            )
        except AuthorizationError:
            return ToolResponse.failure(
                _REPORT_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[_GRANTED_TOOL],
            )
        scope = ReportScope(projects=tuple(targets), all_projects=False)
        async with pool.connection() as conn:
            try:
                response = await _build_report(
                    conn,
                    scope,
                    parsed_window,
                    parsed_formats,
                    secret_registry=secret_registry,
                    store_factory=store_factory,
                    scope_label=_GRANTED_SCOPE,
                    next_tool=_GRANTED_TOOL,
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(
                    _REPORT_OBJECT_ID, exc, suggested_next_actions=[_GRANTED_TOOL]
                )
            if len(targets) > 1:
                await _audit_granted(conn, ctx, targets, parsed_window, parsed_formats)
        return response


async def generate_all_projects(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    secret_registry: SecretRegistry,
    window: object = None,
    formats: list[str] | None = None,
    store_factory: StoreFactory = object_store_from_env,
) -> ToolResponse:
    """Generate a platform-wide report over every project (``platform_auditor``)."""
    with bind_context(principal=ctx.principal):
        try:
            parsed_window = _parse_window(window)
            parsed_formats = _parse_formats(formats)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _REPORT_OBJECT_ID, exc, suggested_next_actions=[_ALL_PROJECTS_TOOL]
            )
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_ALL_PROJECTS_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args=_report_args(ALL_PROJECTS_SCOPE, parsed_window, parsed_formats),
            )
            return ToolResponse.failure(
                _REPORT_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[_ALL_PROJECTS_TOOL],
            )
        async with pool.connection() as conn:
            scope = ReportScope(
                projects=tuple(await _all_projects_universe(conn)), all_projects=True
            )
            try:
                response = await _build_report(
                    conn,
                    scope,
                    parsed_window,
                    parsed_formats,
                    secret_registry=secret_registry,
                    store_factory=store_factory,
                    scope_label=ALL_PROJECTS_SCOPE,
                    next_tool=_ALL_PROJECTS_TOOL,
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(
                    _REPORT_OBJECT_ID, exc, suggested_next_actions=[_ALL_PROJECTS_TOOL]
                )
            await _audit_all_projects(conn, ctx, parsed_window, parsed_formats)
        return response


async def _audit_granted(
    conn: AsyncConnection,
    ctx: RequestContext,
    targets: list[str],
    window: tuple[datetime | None, datetime | None] | None,
    formats: tuple[str, ...],
) -> None:
    scope_value = f"{_GRANTED_SCOPE}:{','.join(sorted(targets))}"
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_GRANTED_TOOL,
                scope=scope_value,
                args=_report_args(_GRANTED_SCOPE, window, formats),
                platform_role=None,
                actor=actor_for(ctx),
            ),
        )


async def _audit_all_projects(
    conn: AsyncConnection,
    ctx: RequestContext,
    window: tuple[datetime | None, datetime | None] | None,
    formats: tuple[str, ...],
) -> None:
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_ALL_PROJECTS_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args=_report_args(ALL_PROJECTS_SCOPE, window, formats),
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


def register(app: FastMCP, pool: AsyncConnectionPool, *, secret_registry: SecretRegistry) -> None:
    """Register the report-generation tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=_GRANTED_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def reports_generate_granted_set(
        projects: Annotated[
            list[str] | None,
            Field(description="Named project subset; omit for all member projects with a role."),
        ] = None,
        window: Annotated[
            list[str | None] | None,
            Field(description="[start, end] ISO-8601 timestamptz pair; omit for all time."),
        ] = None,
        formats: Annotated[
            list[str] | None,
            Field(description="Spreadsheet formats: subset of ['csv','xlsx']; omit for both."),
        ] = None,
    ) -> ToolResponse:
        """Generate a consolidated report over the caller's granted projects."""
        return await generate_granted_set(
            pool,
            current_context(),
            secret_registry=secret_registry,
            projects=projects,
            window=window,
            formats=formats,
        )

    @app.tool(
        name=_ALL_PROJECTS_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def reports_generate_all_projects(
        window: Annotated[
            list[str | None] | None,
            Field(description="[start, end] ISO-8601 timestamptz pair; omit for all time."),
        ] = None,
        formats: Annotated[
            list[str] | None,
            Field(description="Spreadsheet formats: subset of ['csv','xlsx']; omit for both."),
        ] = None,
    ) -> ToolResponse:
        """Generate a platform-wide consolidated report over every project."""
        return await generate_all_projects(
            pool,
            current_context(),
            secret_registry=secret_registry,
            window=window,
            formats=formats,
        )

"""The ``reports.generate`` MCP tool (ADR-0212, ADR-0467).

One tool serves two explicit scopes, selected by a discriminated ``request`` model and
mirroring the ``accounting.report`` split: **granted-set** (the caller's granted
projects, ``viewer`` floor, resolved from the request context) and **all-projects**
(``platform_auditor``, universe read from SQL). The platform-role gate is the first
statement of the all-projects branch, so ``scope`` selects a branch and never grants it.

Either branch captures one ``as_of`` snapshot, gathers the section registry, redacts free
text, returns the report inline within a per-section + byte budget, and writes the
CSV/XLSX spreadsheets to the object store (presigned URLs in ``refs``). A store outage
degrades to inline-only rather than failing the read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
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
from kdive.mcp.platform_auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._time_window import parse_timestamptz_window
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    RoleDenied,
    require_platform_role,
    require_role,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue
from kdive.services.reports.artifacts import ReportArtifactStore, write_report_artifacts
from kdive.services.reports.core import Report, ReportScope, Row, Section, generate_report
from kdive.services.reports.sections import registry

_REPORT_OBJECT_ID = "report"
_TOOL = "reports.generate"
_GRANTED_SCOPE = "granted-set"
_VALID_FORMATS = ("csv", "xlsx")
_WINDOW_DESCRIPTION = "[start, end] ISO-8601 timestamptz pair; omit for all time."
_FORMATS_DESCRIPTION = "Spreadsheet formats: subset of ['csv','xlsx']; omit for both."

StoreFactory = Callable[[], ReportArtifactStore]


class GrantedSetGenerateRequest(ToolPayload):
    """Granted-set report request: the caller's own projects, ``viewer`` floor."""

    scope: Literal["granted-set"]
    projects: list[str] | None = Field(
        default=None,
        description="Named project subset; omit for all member projects with a role.",
    )
    window: list[str | None] | None = Field(default=None, description=_WINDOW_DESCRIPTION)
    formats: list[str] | None = Field(default=None, description=_FORMATS_DESCRIPTION)


class AllProjectsGenerateRequest(ToolPayload):
    """Cross-project report request: every project, ``platform_auditor`` only."""

    scope: Literal["all-projects"]
    window: list[str | None] | None = Field(default=None, description=_WINDOW_DESCRIPTION)
    formats: list[str] | None = Field(default=None, description=_FORMATS_DESCRIPTION)


type GenerateReportRequest = GrantedSetGenerateRequest | AllProjectsGenerateRequest


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
        suggested_next_actions=[_TOOL],
        refs=refs,
        data=data,
    )


def _report_args(
    scope: str, window: tuple[datetime | None, datetime | None] | None, formats: tuple[str, ...]
) -> dict[str, object]:
    return {"scope": scope, "window": _window_data(window), "formats": list(formats)}


async def generate(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    secret_registry: SecretRegistry,
    request: GenerateReportRequest,
    store_factory: StoreFactory,
) -> ToolResponse:
    """Dispatch the typed ``reports.generate`` request model to its explicit handler."""
    if isinstance(request, AllProjectsGenerateRequest):
        return await generate_all_projects(
            pool,
            ctx,
            secret_registry=secret_registry,
            window=request.window,
            formats=request.formats,
            store_factory=store_factory,
        )
    return await generate_granted_set(
        pool,
        ctx,
        secret_registry=secret_registry,
        projects=request.projects,
        window=request.window,
        formats=request.formats,
        store_factory=store_factory,
    )


async def generate_granted_set(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    secret_registry: SecretRegistry,
    projects: list[str] | None = None,
    window: object = None,
    formats: list[str] | None = None,
    store_factory: StoreFactory,
) -> ToolResponse:
    """Generate a report over the caller's granted projects (``viewer`` floor)."""
    with bind_context(principal=ctx.principal):
        try:
            parsed_window = _parse_window(window)
            parsed_formats = _parse_formats(formats)
            targets = _resolve_granted_targets(ctx, projects)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _REPORT_OBJECT_ID, exc, suggested_next_actions=[_TOOL]
            )
        except RoleDenied:
            # The member over-reach. `DenialAuditMiddleware` is the one place ADR-0062 §5's
            # `audit_log` row is written, and it only sees a denial that keeps propagating, so
            # this arm must re-raise rather than envelope (ADR-0508, amending ADR-0493). The
            # boundary rebuilds the same envelope and still names the role (ADR-0490).
            raise
        except AuthorizationError:
            # The non-member arm. Naming `viewer` here would confirm the named project exists
            # and is simply not granted, which ADR-0123's seam exists to prevent.
            return ToolResponse.denied(_REPORT_OBJECT_ID)
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
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(
                    _REPORT_OBJECT_ID, exc, suggested_next_actions=[_TOOL]
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
    store_factory: StoreFactory,
) -> ToolResponse:
    """Generate a platform-wide report over every project (``platform_auditor``)."""
    with bind_context(principal=ctx.principal):
        try:
            parsed_window = _parse_window(window)
            parsed_formats = _parse_formats(formats)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _REPORT_OBJECT_ID, exc, suggested_next_actions=[_TOOL]
            )
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args=_report_args(ALL_PROJECTS_SCOPE, parsed_window, parsed_formats),
            )
            return ToolResponse.denied(
                _REPORT_OBJECT_ID, missing_roles=[PlatformRole.PLATFORM_AUDITOR]
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
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(
                    _REPORT_OBJECT_ID, exc, suggested_next_actions=[_TOOL]
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
                tool=_TOOL,
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
                tool=_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args=_report_args(ALL_PROJECTS_SCOPE, window, formats),
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    secret_registry: SecretRegistry,
    store_factory: StoreFactory,
) -> None:
    """Register the ``reports.generate`` tool on ``app``, bound to ``pool``."""

    @app.tool(
        name=_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def reports_generate(
        request: Annotated[
            GenerateReportRequest,
            Field(
                discriminator="scope",
                description=(
                    "Which projects the report covers: {'scope':'granted-set'} for your "
                    "own projects (optionally narrowed by 'projects'), or "
                    "{'scope':'all-projects'} for every project on the platform."
                ),
            ),
        ],
    ) -> ToolResponse:
        """Generate a downloadable multi-section report over projects you can see.

        ``scope='granted-set'`` covers the projects you hold a role on — omit ``projects``
        for all of them, or name a subset (each is checked for ``viewer``).
        ``scope='all-projects'`` covers every project on the platform and requires
        ``platform_auditor``; without it the call is denied, so ``scope`` selects the
        report, it never grants access to one.

        Either way the tool captures one ``as_of`` snapshot, returns the sections inline
        (within a byte budget) and writes CSV/XLSX spreadsheets to the object store; the
        presigned download URLs land in ``refs``. A store outage degrades to inline-only.
        For a quick inline KCU spend rollup with no spreadsheets, use
        ``accounting.report``.
        """
        return await generate(
            pool,
            current_context(),
            secret_registry=secret_registry,
            request=request,
            store_factory=store_factory,
        )

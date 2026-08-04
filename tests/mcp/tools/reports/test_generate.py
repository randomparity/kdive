"""``reports.generate`` handler tests — scope, RBAC, output shape, store degrade (ADR-0208).

Every call goes through the real dispatcher (:func:`generate`) with a request model parsed
from raw arguments, so the discriminated branch selection the MCP wrapper performs is what
the tests exercise (ADR-0467).
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.reports import generate as generate_module
from kdive.mcp.tools.reports.generate import (
    AllProjectsGenerateRequest,
    GenerateReportRequest,
    GrantedSetGenerateRequest,
    StoreFactory,
    generate,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role, RoleDenied
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.reports.artifacts import ReportArtifactStore
from kdive.services.reports.core import Report, ReportScope, Section

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
_SECTIONS = {"inventory", "leases", "images", "activity", "costs"}
_SECRET = "report-owned-registry-secret"  # pragma: allowlist secret  (planted test value)


class _FakeStore:
    """Records puts and mints deterministic presigned URLs."""

    def __init__(self) -> None:
        self.puts: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.puts.append(request)
        return StoredArtifact(
            key=request.key(),
            etag="etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
            version_id="test-version",
        )

    def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://signed.test/{key}"


def _store_factory() -> ReportArtifactStore:
    return _FakeStore()


def _failing_factory() -> ReportArtifactStore:
    raise CategorizedError(
        "object store unconfigured",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={},
    )


def _ctx(
    *,
    projects: tuple[str, ...] = ("proj",),
    role: Role | None = Role.VIEWER,
    platform: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    roles = {p: role for p in projects} if role is not None else {}
    return RequestContext(
        principal="user-1",
        agent_session="s",
        projects=projects,
        roles=roles,
        platform_roles=platform,
    )


def _secret_registry() -> SecretRegistry:
    return SecretRegistry()


async def _generate(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    scope: str = "granted-set",
    projects: list[str] | None = None,
    window: list[str | None] | None = None,
    formats: list[str] | None = None,
    store_factory: StoreFactory,
) -> ToolResponse:
    """Drive ``reports.generate`` through the real dispatcher with a parsed request model.

    Parsing the raw argument dict through the discriminated union is what the MCP wrapper
    does, so the tests exercise the same isinstance branch selection (ADR-0467) rather than
    calling a branch handler the caller could never have reached.
    """
    payload: dict[str, object] = {"scope": scope, "window": window, "formats": formats}
    if scope == "granted-set":
        payload["projects"] = projects
    request = TypeAdapter(GenerateReportRequest).validate_python(payload)
    return await generate(
        pool,
        ctx,
        secret_registry=_secret_registry(),
        request=request,
        store_factory=store_factory,
    )


def test_registered_wrapper_passes_injected_store_factory_to_generate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("reports-registrar-test")
    ctx = _ctx()
    registry = SecretRegistry()
    store_factory = _store_factory
    captured: dict[str, object] = {}

    async def _generate_report(
        registered_pool: AsyncConnectionPool,
        registered_ctx: RequestContext,
        *,
        secret_registry: SecretRegistry,
        request: GenerateReportRequest,
        store_factory: StoreFactory,
    ) -> ToolResponse:
        captured["pool"] = registered_pool
        captured["ctx"] = registered_ctx
        captured["secret_registry"] = secret_registry
        captured["request"] = request
        captured["store_factory"] = store_factory
        return ToolResponse.success("report", "generated")

    monkeypatch.setattr(generate_module, "current_context", lambda: ctx)
    monkeypatch.setattr(generate_module, "generate", _generate_report)
    generate_module.register(app, pool, secret_registry=registry, store_factory=store_factory)
    tool = next(tool for tool in asyncio.run(app.list_tools()) if tool.name == "reports.generate")

    response = asyncio.run(
        cast(FunctionTool, tool).fn(GrantedSetGenerateRequest(scope="granted-set"))
    )

    assert response.status == "generated"
    assert captured == {
        "pool": pool,
        "ctx": ctx,
        "secret_registry": registry,
        "request": GrantedSetGenerateRequest(scope="granted-set"),
        "store_factory": store_factory,
    }


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(pool: AsyncConnectionPool, project: str = "proj") -> None:
    async with pool.connection() as conn, conn.transaction():
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_AS_OF,
                updated_at=_AS_OF,
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities={},
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(conn, _allocation(res.id, project))
        await SYSTEMS.insert(conn, _system(alloc.id, project))


def _allocation(resource_id, project):  # noqa: ANN001, ANN202
    from kdive.domain.lifecycle.records import Allocation

    return Allocation(
        id=uuid4(),
        created_at=_AS_OF,
        updated_at=_AS_OF,
        principal="user-1",
        project=project,
        resource_id=resource_id,
        state=AllocationState.ACTIVE,
    )


def _system(allocation_id, project):  # noqa: ANN001, ANN202
    from kdive.domain.lifecycle.records import System

    return System(
        id=uuid4(),
        created_at=_AS_OF,
        updated_at=_AS_OF,
        principal="user-1",
        project=project,
        allocation_id=allocation_id,
        state=SystemState.READY,
        provisioning_profile={},
    )


def test_granted_set_viewer_returns_all_sections_and_refs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(
                pool, _ctx(), formats=["csv", "xlsx"], store_factory=_store_factory
            )
        assert resp.status == "ok"
        assert {item.data["section"] for item in resp.items} == _SECTIONS
        assert "xlsx" in resp.refs
        assert any(key.startswith("csv:") for key in resp.refs)
        assert resp.data["scope"] == "granted-set"

    asyncio.run(_run())


def test_granted_set_role_less_named_project_propagates_role_denied(migrated_url: str) -> None:
    """A *member* of "proj" holding no role: `RoleDenied` leaves the handler unenveloped.

    The handler deliberately does **not** answer this one. `DenialAuditMiddleware` is the only
    place ADR-0062 §5's `audit_log` row is written and it sees a denial only if the denial keeps
    propagating, so the arm re-raises (ADR-0508, amending ADR-0493). This module calls the
    handler directly, below every middleware, so the exception is all it can observe; the
    envelope the caller actually receives — and the audit row — are pinned over a real dispatch
    by `test_roleless_member_named_project_generate_is_audited_at_the_dispatch_boundary` in
    `tests/mcp/tools/test_gateway_usage_recording_e2e.py`.
    """

    async def _run() -> RoleDenied:
        async with _pool(migrated_url) as pool:
            with pytest.raises(RoleDenied) as excinfo:
                await _generate(
                    pool,
                    _ctx(role=None),
                    projects=["proj"],
                    formats=["csv"],
                    store_factory=_store_factory,
                )
        return excinfo.value

    denial = asyncio.run(_run())
    assert denial.required == Role.VIEWER
    assert denial.project == "proj"


def test_granted_set_non_member_named_project_names_no_role(migrated_url: str) -> None:
    """The non-member arm discloses nothing: naming a role would confirm the project exists.

    The counterpart to the test above, and the reason the two arms are separate `except`
    clauses rather than one. `require_role` raises the base `AuthorizationError` here — never
    `RoleDenied`, which fires only past the membership check — so `other-proj` gets a denial
    byte-identical to one for a project that does not exist at all (ADR-0123, ADR-0490).
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _generate(
                pool,
                _ctx(),
                projects=["other-proj"],
                formats=["csv"],
                store_factory=_store_factory,
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert "missing_roles" not in resp.data
        assert "other-proj" not in resp.model_dump_json()

    asyncio.run(_run())


def test_all_projects_requires_platform_auditor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            denied = await _generate(
                pool,
                _ctx(),
                scope="all-projects",
                formats=["csv"],
                store_factory=_store_factory,
            )
            ok = await _generate(
                pool,
                _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR})),
                scope="all-projects",
                formats=["csv"],
                store_factory=_store_factory,
            )
        assert denied.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert ok.status == "ok"
        assert ok.data["scope"] == "all-projects"

    asyncio.run(_run())


def test_empty_formats_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _generate(pool, _ctx(), formats=[], store_factory=_store_factory)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_store_outage_degrades_to_inline(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(
                pool, _ctx(), formats=["csv", "xlsx"], store_factory=_failing_factory
            )
        assert resp.status == "ok"
        assert resp.refs == {}
        assert resp.data["spreadsheet_unavailable"] == "store_error"
        assert {item.data["section"] for item in resp.items} == _SECTIONS

    asyncio.run(_run())


def test_missing_xlsx_dependency_is_not_reported_as_store_error(
    migrated_url: str, monkeypatch
) -> None:  # noqa: ANN001
    real_import = importlib.import_module

    def missing_openpyxl(name: str, package: str | None = None) -> object:
        if name == "openpyxl":
            raise ImportError("missing")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", missing_openpyxl)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(pool, _ctx(), formats=["xlsx"], store_factory=_store_factory)
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.MISSING_DEPENDENCY.value
        assert resp.data["dependency"] == "openpyxl"
        assert "spreadsheet_unavailable" not in resp.data

    asyncio.run(_run())


def test_build_report_redacts_with_app_owned_registry(monkeypatch) -> None:  # noqa: ANN001
    registry = SecretRegistry()
    registry.register(_SECRET, scope="reports-test")

    async def _now(_conn) -> datetime:  # noqa: ANN001
        return _AS_OF

    async def _generate_report(
        _conn,
        _scope: ReportScope,
        _window,
        _as_of: datetime,
        *,
        sections,  # noqa: ANN001
    ) -> Report:
        return Report(
            sections=(
                Section(
                    key="inventory",
                    columns=("note",),
                    rows=({"note": f"prefix {_SECRET} suffix"},),
                    truncated=False,
                ),
            ),
            as_of=_as_of,
        )

    async def _run() -> None:
        monkeypatch.setattr(generate_module, "_now", _now)
        monkeypatch.setattr(generate_module, "generate_report", _generate_report)
        response = await generate_module._build_report(
            cast(AsyncConnection, object()),
            ReportScope(projects=("proj",), all_projects=False),
            None,
            ("csv",),
            secret_registry=registry,
            store_factory=_failing_factory,
            scope_label="granted-set",
        )

        rows = response.items[0].data["rows_json"]
        assert isinstance(rows, str)
        assert _SECRET not in rows
        assert REDACTION in rows

    asyncio.run(_run())


# ---- ADR-0467 branch-authorization matrix ------------------------------------------

_PROJECT_ROLES = {
    "project_viewer": Role.VIEWER,
    "project_contributor": Role.CONTRIBUTOR,
    "project_admin": Role.ADMIN,
}
_PLATFORM_ROLES = {
    "platform_operator": PlatformRole.PLATFORM_OPERATOR,
    "platform_admin": PlatformRole.PLATFORM_ADMIN,
    "platform_auditor": PlatformRole.PLATFORM_AUDITOR,
}


def _matrix_ctx(identity: str) -> RequestContext:
    """The RequestContext for one identity in the matrix ('unauthorized' holds nothing)."""
    project_role = _PROJECT_ROLES.get(identity)
    if project_role is not None:
        return _ctx(role=project_role)
    platform_role = _PLATFORM_ROLES.get(identity)
    if platform_role is not None:
        return _ctx(projects=(), role=None, platform=frozenset({platform_role}))
    return _ctx(projects=(), role=None)


@pytest.mark.parametrize("identity", ["platform_auditor", "platform_admin"])
def test_matrix_all_projects_allowed(migrated_url: str, identity: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(
                pool,
                _matrix_ctx(identity),
                scope="all-projects",
                formats=["csv"],
                store_factory=_store_factory,
            )
        assert resp.status == "ok"
        assert resp.data["scope"] == "all-projects"

    asyncio.run(_run())


@pytest.mark.parametrize(
    "identity",
    [
        "project_viewer",
        "project_contributor",
        "project_admin",
        "platform_operator",
        "unauthorized",
    ],
)
def test_matrix_all_projects_denied(migrated_url: str, identity: str) -> None:
    # The gate is the first statement of the all-projects branch, so sending
    # scope='all-projects' selects that branch and is refused there — no sections, no refs.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(
                pool,
                _matrix_ctx(identity),
                scope="all-projects",
                formats=["csv"],
                store_factory=_store_factory,
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert resp.items == []
        assert resp.refs == {}

    asyncio.run(_run())


@pytest.mark.parametrize("identity", ["project_viewer", "project_contributor", "project_admin"])
def test_matrix_granted_set_allowed(migrated_url: str, identity: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(
                pool, _matrix_ctx(identity), formats=["csv"], store_factory=_store_factory
            )
        assert resp.status == "ok"
        assert resp.data["scope"] == "granted-set"

    asyncio.run(_run())


@pytest.mark.parametrize(
    "identity",
    ["platform_operator", "platform_admin", "platform_auditor", "unauthorized"],
)
def test_matrix_granted_set_without_project_grant_names_no_project(
    migrated_url: str, identity: str
) -> None:
    # A platform role is not a project grant: the granted-set branch resolves from
    # ctx.projects and never the SQL universe, so the report spans no project.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await _generate(
                pool, _matrix_ctx(identity), formats=["csv"], store_factory=_store_factory
            )
        assert resp.status == "ok"
        assert resp.data["scope"] == "granted-set"
        assert all(item.data["count"] == 0 for item in resp.items)

    asyncio.run(_run())


def test_viewer_supplied_all_projects_scope_is_denied_at_the_branch(migrated_url: str) -> None:
    """Agent-supplied ``scope='all-projects'`` selects the branch; the branch denies it."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            request = TypeAdapter(GenerateReportRequest).validate_python(
                {"scope": "all-projects", "formats": ["csv"]}
            )
            assert isinstance(request, AllProjectsGenerateRequest)
            resp = await generate(
                pool,
                _ctx(),
                secret_registry=_secret_registry(),
                request=request,
                store_factory=_store_factory,
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert resp.items == []

    asyncio.run(_run())


# ---- audit attribution under one tool name (ADR-0467) ------------------------------


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


def test_audit_rows_stay_distinguishable_under_one_tool_name(migrated_url: str) -> None:
    """Both scopes audit as ``reports.generate``; ``scope`` + ``platform_role`` separate them."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            multi = _ctx(projects=("proj", "proj-b"))
            assert (
                await _generate(pool, multi, formats=["csv"], store_factory=_store_factory)
            ).status == "ok"
            auditor = _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            assert (
                await _generate(
                    pool,
                    auditor,
                    scope="all-projects",
                    formats=["csv"],
                    store_factory=_store_factory,
                )
            ).status == "ok"
        rows = sorted(await _platform_audit_rows(migrated_url), key=lambda r: str(r[3]))
        assert [r[2] for r in rows] == ["reports.generate", "reports.generate"]
        assert rows[0][3] == "all-projects"
        assert rows[0][1] == "platform_auditor"
        assert rows[1][3] == "granted-set:proj,proj-b"
        assert rows[1][1] is None

    asyncio.run(_run())


def test_single_project_granted_set_is_not_audited(migrated_url: str) -> None:
    # The granted-set audit stays conditional (>1 target); a caller reading only its own
    # single project writes no platform_audit_log row.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            assert (
                await _generate(pool, _ctx(), formats=["csv"], store_factory=_store_factory)
            ).status == "ok"
        assert await _platform_audit_rows(migrated_url) == []

    asyncio.run(_run())


def test_all_projects_denial_audited_only_with_a_platform_role(migrated_url: str) -> None:
    # ADR-0043 §4: a project-only token's denial is the routine non-grant case and is not
    # recorded; a platform_operator's over-reach is.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            denied = await _generate(
                pool,
                _ctx(),
                scope="all-projects",
                formats=["csv"],
                store_factory=_store_factory,
            )
            assert denied.status == "error"
            assert await _platform_audit_rows(migrated_url) == []
            operator = _ctx(
                projects=(), role=None, platform=frozenset({PlatformRole.PLATFORM_OPERATOR})
            )
            assert (
                await _generate(
                    pool,
                    operator,
                    scope="all-projects",
                    formats=["csv"],
                    store_factory=_store_factory,
                )
            ).status == "error"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"
        assert rows[0][2] == "reports.generate"
        assert rows[0][3] == "all-projects"

    asyncio.run(_run())

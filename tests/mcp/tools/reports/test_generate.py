"""reports.generate_* handler tests — scope, RBAC, output shape, store degrade (ADR-0208)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools.reports.generate import generate_all_projects, generate_granted_set
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.reports.artifacts import ReportArtifactStore

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
_SECTIONS = {"inventory", "leases", "images", "activity", "costs"}


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
        )

    def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://signed.test/{key}"

    def delete(self, key: str) -> None:  # pragma: no cover - unused in these tests
        pass


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
            resp = await generate_granted_set(
                pool,
                _ctx(),
                projects=None,
                window=None,
                formats=["csv", "xlsx"],
                store_factory=_store_factory,
            )
        assert resp.status == "ok"
        assert {item.data["section"] for item in resp.items} == _SECTIONS
        assert "xlsx" in resp.refs
        assert any(key.startswith("csv:") for key in resp.refs)
        assert resp.data["scope"] == "granted-set"

    asyncio.run(_run())


def test_granted_set_role_less_named_project_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await generate_granted_set(
                pool,
                _ctx(role=None),
                projects=["proj"],
                window=None,
                formats=["csv"],
                store_factory=_store_factory,
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value

    asyncio.run(_run())


def test_all_projects_requires_platform_auditor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            denied = await generate_all_projects(
                pool, _ctx(), window=None, formats=["csv"], store_factory=_store_factory
            )
            ok = await generate_all_projects(
                pool,
                _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR})),
                window=None,
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
            resp = await generate_granted_set(
                pool,
                _ctx(),
                projects=None,
                window=None,
                formats=[],
                store_factory=_store_factory,
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_store_outage_degrades_to_inline(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_system(pool)
            resp = await generate_granted_set(
                pool,
                _ctx(),
                projects=None,
                window=None,
                formats=["csv", "xlsx"],
                store_factory=_failing_factory,
            )
        assert resp.status == "ok"
        assert resp.refs == {}
        assert resp.data["spreadsheet_unavailable"] == "store_error"
        assert {item.data["section"] for item in resp.items} == _SECTIONS

    asyncio.run(_run())

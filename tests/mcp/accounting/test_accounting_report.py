"""``accounting.report`` tests — one tool, two discriminated scopes + read-shape audit.

Every call goes through the real dispatcher (:func:`report`) with a parsed request model,
so the tests exercise the same isinstance branch selection the MCP wrapper does — a caller
asking for a scope it cannot have lands in that scope's own gate. The handler is called
directly with an injected pool + RequestContext (the repo's unit contract). Coverage maps
to the #97 acceptance bullets plus the ADR-0467 branch-authorization matrix:

* all-projects: platform_auditor / platform_admin rollup ≥2 projects, always audited;
  SoD denials (project viewer/contributor/admin token; platform_operator; platform_admin
  passes) — denial audited iff the caller holds ≥1 platform role.
* granted-set: default to ctx.projects (role-less membership dropped), named non-member
  denied with an envelope and named role-less membership re-raised as ``RoleDenied``
  (ADR-0493), zero-project empty rollup, audit-iff-shape (>1 project OR
  group_by=principal), never a per-project audit_log row.
* group_by=principal over a window, in both scope forms.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.accounting.reports import (
    AccountingAllProjectsReportRequest,
    AccountingGrantedSetReportRequest,
    AccountingReportRequest,
    report,
)
from kdive.security.authz.rbac import PlatformRole, Role, RoleDenied
from tests.mcp.json_data import data_str

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_BASE_CTX = RequestContext(principal="user-1", agent_session="sess-1", projects=())


def _platform_ctx(role: PlatformRole) -> RequestContext:
    return replace(_BASE_CTX, platform_roles=frozenset({role}))


def _project_ctx(*, roles: dict[str, Role], projects: tuple[str, ...]) -> RequestContext:
    return replace(_BASE_CTX, projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _resource(conn: psycopg.AsyncConnection) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    return res.id


async def _alloc(
    conn: psycopg.AsyncConnection, resource_id: UUID, project: str, principal: str
) -> UUID:
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal=principal,
            project=project,
            resource_id=resource_id,
            state=AllocationState.ACTIVE,
            requested_vcpus=2,
            requested_memory_gb=4,
        ),
    )
    return alloc.id


async def _ledger(
    conn: psycopg.AsyncConnection,
    project: str,
    alloc_id: UUID,
    event_type: str,
    kcu: str,
    ts: datetime = _DT,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ledger (id, ts, project, allocation_id, cost_class, "
            "event_type, kcu_delta) VALUES (%s, %s, %s, %s, 'local', %s, %s)",
            (uuid4(), ts, project, alloc_id, event_type, Decimal(kcu)),
        )


async def _budget(conn: psycopg.AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 1000, 0)",
            (project,),
        )


async def _seed_two_projects(pool: AsyncConnectionPool) -> None:
    """proj-a (alice: +10/-3) and proj-b (bob: +20/+5), both with budget rows."""
    async with pool.connection() as conn:
        res = await _resource(conn)
        await _budget(conn, "proj-a")
        await _budget(conn, "proj-b")
        a = await _alloc(conn, res, "proj-a", "alice")
        b = await _alloc(conn, res, "proj-b", "bob")
        await _ledger(conn, "proj-a", a, "reserved", "10")
        await _ledger(conn, "proj-a", a, "reconciled", "-3")
        await _ledger(conn, "proj-b", b, "reserved", "20")
        await _ledger(conn, "proj-b", b, "reconciled", "5")


async def _count_platform_audit(url: str) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _count_audit_log(url: str) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


def _rows(resp: ToolResponse) -> list[dict[str, object]]:
    return [cast(dict[str, object], item.data) for item in resp.items]


def _total(resp: ToolResponse) -> dict[str, str]:
    return {
        "project": data_str(resp, "total_project"),
        "principal": data_str(resp, "total_principal"),
        "reserved": data_str(resp, "total_reserved"),
        "reconciled": data_str(resp, "total_reconciled"),
        "variance": data_str(resp, "total_variance"),
    }


async def report_granted_set(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    **kwargs: object,
) -> ToolResponse:
    """Drive the merged tool at ``scope='granted-set'`` through the real dispatcher."""
    request = AccountingGrantedSetReportRequest.model_validate({"scope": "granted-set", **kwargs})
    return await report(pool, ctx, request=request)


async def report_all_projects(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    **kwargs: object,
) -> ToolResponse:
    """Drive the merged tool at ``scope='all-projects'`` through the real dispatcher."""
    request = AccountingAllProjectsReportRequest.model_validate({"scope": "all-projects", **kwargs})
    return await report(pool, ctx, request=request)


# ---- all-projects form ------------------------------------------------------------


def test_all_projects_auditor_rollup_and_audit_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "ok"
        assert resp.error_category is None
        by_project = {r["project"]: r for r in _rows(resp)}
        assert by_project["proj-a"]["variance"] == "-13.0000"
        assert by_project["proj-b"]["variance"] == "-15.0000"
        total = _total(resp)
        assert total["reserved"] == "30.0000"
        assert total["reconciled"] == "2.0000"
        assert total["variance"] == "-28.0000"
        # Exactly one platform_audit_log row (role recorded), zero per-project audit_log.
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("user-1", "platform_auditor", "accounting.report", "all-projects")]
        assert await _count_audit_log(migrated_url) == 0

    asyncio.run(_run())


def test_all_projects_admin_satisfies_auditor_gate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_ADMIN)
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"

    asyncio.run(_run())


def test_all_projects_project_only_token_denied_unaudited(migrated_url: str) -> None:
    # SoD: a project-scoped admin holds no platform role → denied, and the denial is NOT
    # audited (routine non-grant on an openly-callable read; no write amplification).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["session.whoami"]
        assert "accounting.report" not in resp.suggested_next_actions  # ADR-0471, #1596
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_all_projects_operator_denied_but_audited(migrated_url: str) -> None:
    # SoD: platform_operator does NOT satisfy the auditor gate, but holds a platform role,
    # so the over-reach denial IS audited (the accountability target).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_OPERATOR)
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["session.whoami"]
        assert "accounting.report" not in resp.suggested_next_actions  # ADR-0471, #1596
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"
        assert rows[0][3] == "all-projects"

    asyncio.run(_run())


# ---- granted-set form -------------------------------------------------------------


def test_granted_set_default_resolves_member_projects_with_role(migrated_url: str) -> None:
    # viewer on A+B, bare member of C → rollup over exactly A+B (C dropped); one audit row
    # (>1 project, platform_role null); no per-project audit_log row.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(
                roles={"proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-a", "proj-b", "proj-c"),
            )
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a", "proj-b"}
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] is None  # platform_role null for a member read
        assert rows[0][3] == "granted-set:proj-a,proj-b"
        assert await _count_audit_log(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_names_both_projects_when_only_one_has_spend(migrated_url: str) -> None:
    # The granted set names every authorized project (#426): A has ledger rows and B does
    # not, but BOTH appear (B zero-filled). The audit trigger still counts the authorized
    # set (A+B), and the audit scope is derived from sorted(targets), not from the rows —
    # so it stays "granted-set:proj-a,proj-b" even though only A has spend.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-a")
                await _budget(conn, "proj-b")
                a = await _alloc(conn, res, "proj-a", "alice")
                await _ledger(conn, "proj-a", a, "reserved", "5")
            ctx = _project_ctx(
                roles={"proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-a", "proj-b"),
            )
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        by_project = {r["project"]: r for r in _rows(resp)}
        assert set(by_project) == {"proj-a", "proj-b"}
        assert by_project["proj-a"]["reserved"] == "5.0000"
        # B is zero-filled, byte-identical to a real zero row.
        assert by_project["proj-b"]["reserved"] == "0.0000"
        assert by_project["proj-b"]["reconciled"] == "0.0000"
        assert by_project["proj-b"]["variance"] == "0.0000"
        assert by_project["proj-b"]["principal"] == ""
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][3] == "granted-set:proj-a,proj-b"

    asyncio.run(_run())


def test_granted_set_zero_spend_project_is_named(migrated_url: str) -> None:
    # The observed #426 bug: a single granted project with no ledger rows was never named
    # (empty items, only total_project="*"). It must now appear as one zero-filled item.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _budget(conn, "proj-a")
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert resp.data["project_count"] == 1
        rows = _rows(resp)
        assert len(rows) == 1
        assert rows[0]["project"] == "proj-a"
        assert rows[0]["reserved"] == "0.0000"
        assert rows[0]["reconciled"] == "0.0000"
        assert rows[0]["variance"] == "0.0000"

    asyncio.run(_run())


def test_granted_set_zero_fill_is_deterministically_ordered(migrated_url: str) -> None:
    # Three granted projects, none with spend: all are zero-filled and the items come back
    # sorted by project name (stable across runs).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _project_ctx(
                roles={"proj-c": Role.VIEWER, "proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-c", "proj-a", "proj-b"),
            )
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert [r["project"] for r in _rows(resp)] == ["proj-a", "proj-b", "proj-c"]

    asyncio.run(_run())


def test_granted_set_mixed_spent_and_zero_is_ordered(migrated_url: str) -> None:
    # A granted set mixing a spent project (proj-b) with two unspent ones returns items
    # ordered by project name — the whole set, not just the zero-fill tail (the domain
    # rollup query is unordered, so _name_targets sorts).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-b")
                b = await _alloc(conn, res, "proj-b", "bob")
                await _ledger(conn, "proj-b", b, "reserved", "9")
            ctx = _project_ctx(
                roles={"proj-b": Role.VIEWER, "proj-a": Role.VIEWER, "proj-c": Role.VIEWER},
                projects=("proj-b", "proj-a", "proj-c"),
            )
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert [r["project"] for r in _rows(resp)] == ["proj-a", "proj-b", "proj-c"]
        by_project = {r["project"]: r for r in _rows(resp)}
        assert by_project["proj-b"]["reserved"] == "9.0000"
        assert by_project["proj-a"]["reserved"] == "0.0000"

    asyncio.run(_run())


def test_granted_set_duplicate_target_names_project_once(migrated_url: str) -> None:
    # ctx.projects is not deduplicated upstream; a duplicated unspent project must still
    # produce exactly one zero-filled item, not one per duplicate.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a", "proj-a"))
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        rows = _rows(resp)
        assert [r["project"] for r in rows] == ["proj-a"]

    asyncio.run(_run())


def test_granted_set_group_by_principal_names_zero_spend_project(migrated_url: str) -> None:
    # group_by=principal over {proj-a (alice spend), proj-b (none)}: proj-a breaks down per
    # principal, proj-b appears once with an empty principal (its item id is the bare name).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-a")
                await _budget(conn, "proj-b")
                a = await _alloc(conn, res, "proj-a", "alice")
                await _ledger(conn, "proj-a", a, "reserved", "7")
            ctx = _project_ctx(
                roles={"proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-a", "proj-b"),
            )
            resp = await report_granted_set(pool, ctx, group_by="principal")
        assert resp.status == "ok"
        rows = _rows(resp)
        proj_b = [r for r in rows if r["project"] == "proj-b"]
        assert len(proj_b) == 1
        assert proj_b[0]["principal"] == ""
        assert proj_b[0]["reserved"] == "0.0000"
        by_principal = {r["principal"]: r for r in rows if r["project"] == "proj-a"}
        assert by_principal["alice"]["reserved"] == "7.0000"

    asyncio.run(_run())


def test_granted_set_window_excludes_spend_names_zero(migrated_url: str) -> None:
    # A granted project with ledger rows only OUTSIDE the requested window is named with
    # zeros inside it (zero-fill keys off "no rows in the window", not "never any spend").
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-a")
                a = await _alloc(conn, res, "proj-a", "alice")
                outside = datetime(2026, 3, 1, tzinfo=UTC)
                await _ledger(conn, "proj-a", a, "reserved", "100", outside)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            window = ["2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"]
            resp = await report_granted_set(pool, ctx, window=window)
        assert resp.status == "ok"
        rows = _rows(resp)
        assert len(rows) == 1
        assert rows[0]["project"] == "proj-a"
        assert rows[0]["reserved"] == "0.0000"

    asyncio.run(_run())


def test_granted_set_all_roleless_memberships_empty_rollup(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={}, projects=("proj-a", "proj-b"))
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert _total(resp)["reserved"] == "0.0000"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_named_non_member_denied_with_an_envelope(migrated_url: str) -> None:
    """A named non-member project is denied in-band, not by raising (#1661, ADR-0493).

    ``require_role``'s non-member site raises the **base** ``AuthorizationError``, which
    ``DenialAuditMiddleware`` does not own, so letting it escape reached the client as a raw
    ``ToolError`` and metered as ``error``. The handler envelopes it instead. No role is named:
    ``viewer`` here would confirm ``proj-z`` exists (ADR-0123).
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx, projects=["proj-a", "proj-z"])
            assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
            assert resp.object_id == "report"
            assert "missing_roles" not in resp.data
            # Denied before any rollup ran, so the granted half of the named set leaked nothing.
            assert _rows(resp) == []
            assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_named_roleless_project_keeps_raising_role_denied(migrated_url: str) -> None:
    """A named role-less *membership* still propagates, so the boundary can audit it.

    The counterpart to the test above and the reason ADR-0493's catch is two arms rather than
    one: ``RoleDenied`` is re-raised so ``DenialAuditMiddleware`` writes ADR-0062 §5's
    ``audit_log`` row and names the role. Widening the handler's catch to the base class alone
    would swallow both this raise and that row.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            # proj-c is a bare membership (no role): naming it explicitly must raise.
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a", "proj-c"))
            with pytest.raises(RoleDenied) as raised:
                await report_granted_set(pool, ctx, projects=["proj-c"])
            assert raised.value.project == "proj-c"
            assert raised.value.required is Role.VIEWER

    asyncio.run(_run())


def test_granted_set_single_project_ungrouped_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a"}
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_single_project_group_by_principal_audited(migrated_url: str) -> None:
    # group_by=principal is the load-bearing audit trigger even for one project.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx, group_by="principal")
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] is None

    asyncio.run(_run())


def test_granted_set_zero_resolution_empty_rollup(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={}, projects=())
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


# ---- group_by=principal over a window, both scope forms --------------------------


def test_group_by_principal_window_totals_granted_set(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-a")
                alice = await _alloc(conn, res, "proj-a", "alice")
                bob = await _alloc(conn, res, "proj-a", "bob")
                inside = datetime(2026, 1, 15, tzinfo=UTC)
                outside = datetime(2026, 3, 1, tzinfo=UTC)
                await _ledger(conn, "proj-a", alice, "reserved", "8", inside)
                await _ledger(conn, "proj-a", bob, "reserved", "3", inside)
                await _ledger(conn, "proj-a", alice, "reserved", "100", outside)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            window = ["2026-01-10T00:00:00+00:00", "2026-02-01T00:00:00+00:00"]
            resp = await report_granted_set(pool, ctx, group_by="principal", window=window)
        assert resp.status == "ok"
        by_principal = {r["principal"]: r for r in _rows(resp)}
        assert by_principal["alice"]["reserved"] == "8.0000"
        assert by_principal["bob"]["reserved"] == "3.0000"

    asyncio.run(_run())


def test_group_by_principal_all_projects(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            resp = await report_all_projects(pool, ctx, group_by="principal")
        assert resp.status == "ok"
        keyed = {(r["project"], r["principal"]): r for r in _rows(resp)}
        assert keyed[("proj-a", "alice")]["reserved"] == "10.0000"
        assert keyed[("proj-b", "bob")]["reserved"] == "20.0000"

    asyncio.run(_run())


# ---- input validation -------------------------------------------------------------


def test_invalid_group_by_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(pool, _BASE_CTX, group_by="project")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.report"]

    asyncio.run(_run())


def test_invalid_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(pool, _BASE_CTX, window=["not-a-date", None])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.report"]

    asyncio.run(_run())


def test_naive_window_bound_is_config_error(migrated_url: str) -> None:
    # ledger.ts is timestamptz; a tz-naive bound must fail closed, not compare in an
    # unintended zone.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(pool, _BASE_CTX, window=["2026-01-01T00:00:00", None])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_inverted_window_is_config_error(migrated_url: str) -> None:
    # start >= end must error rather than return a silently-empty rollup.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(
                pool,
                _BASE_CTX,
                window=["2026-02-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"],
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_granted_set_explicit_empty_list_is_empty_rollup_unaudited(migrated_url: str) -> None:
    # Naming an explicit empty list resolves to zero projects: an empty rollup (success),
    # no audit row (distinct from the default-to-ctx.projects path).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx, projects=[])
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_all_projects_universe_includes_ledger_without_budget(migrated_url: str) -> None:
    # The oversight read must span every project: a project with ledger spend but no budget
    # row is still reported (not dropped from the cross-tenant total).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                # proj-x has ledger rows but NO budget row.
                x = await _alloc(conn, res, "proj-x", "xavier")
                await _ledger(conn, "proj-x", x, "reserved", "42")
            ctx = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "ok"
        by_project = {r["project"]: r for r in _rows(resp)}
        assert by_project["proj-x"]["reserved"] == "42.0000"

    asyncio.run(_run())


# ---- ADR-0467 branch-authorization matrix ------------------------------------------
#
# One tool now serves both scopes, so the matrix has to prove that the *branch* still
# gates: every identity is driven at every scope. `scope` picks a branch; the branch
# performs its own check.

_PROJECT_ROLE_IDENTITIES = {
    "project_viewer": Role.VIEWER,
    "project_contributor": Role.CONTRIBUTOR,
    "project_admin": Role.ADMIN,
}
_PLATFORM_ROLE_IDENTITIES = {
    "platform_operator": PlatformRole.PLATFORM_OPERATOR,
    "platform_admin": PlatformRole.PLATFORM_ADMIN,
    "platform_auditor": PlatformRole.PLATFORM_AUDITOR,
}


def _matrix_ctx(identity: str) -> RequestContext:
    """The RequestContext for one identity in the matrix ('unauthorized' holds nothing)."""
    project_role = _PROJECT_ROLE_IDENTITIES.get(identity)
    if project_role is not None:
        return _project_ctx(
            roles={"proj-a": project_role, "proj-b": project_role},
            projects=("proj-a", "proj-b"),
        )
    platform_role = _PLATFORM_ROLE_IDENTITIES.get(identity)
    if platform_role is not None:
        return _platform_ctx(platform_role)
    return _BASE_CTX


@pytest.mark.parametrize("identity", ["platform_auditor", "platform_admin"])
def test_matrix_all_projects_allowed(migrated_url: str, identity: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            resp = await report_all_projects(pool, _matrix_ctx(identity))
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a", "proj-b"}
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1, "the all-projects read is unconditionally audited"
        assert rows[0][3] == "all-projects"

    asyncio.run(_run())


@pytest.mark.parametrize(
    "identity", ["project_viewer", "project_contributor", "project_admin", "unauthorized"]
)
def test_matrix_all_projects_denied_and_not_audited(migrated_url: str, identity: str) -> None:
    # No platform role at all → denied, and the denial is NOT recorded: auditing it would
    # let any authenticated token amplify writes into platform_audit_log (ADR-0043 §4).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            resp = await report_all_projects(pool, _matrix_ctx(identity))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert _rows(resp) == [], "a denied all-projects read must leak no rows"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


@pytest.mark.parametrize("identity", ["platform_operator"])
def test_matrix_all_projects_denied_but_audited(migrated_url: str, identity: str) -> None:
    # Holds ≥1 platform role but not the auditor gate → denied, and the over-reach IS
    # audited (the accountability target).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            resp = await report_all_projects(pool, _matrix_ctx(identity))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert _rows(resp) == []
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"

    asyncio.run(_run())


@pytest.mark.parametrize("identity", ["project_viewer", "project_contributor", "project_admin"])
def test_matrix_granted_set_rolls_up_own_projects(migrated_url: str, identity: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            resp = await report_granted_set(pool, _matrix_ctx(identity))
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a", "proj-b"}

    asyncio.run(_run())


@pytest.mark.parametrize(
    "identity",
    ["platform_operator", "platform_admin", "platform_auditor", "unauthorized"],
)
def test_matrix_granted_set_without_project_grant_is_empty(
    migrated_url: str, identity: str
) -> None:
    # A platform role is not a project grant: the granted-set branch reads ctx.projects and
    # never the SQL universe, so an auditor with no membership rolls up nothing.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            resp = await report_granted_set(pool, _matrix_ctx(identity))
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert _total(resp)["reserved"] == "0.0000"

    asyncio.run(_run())


def test_viewer_supplied_all_projects_scope_is_denied_at_the_branch(migrated_url: str) -> None:
    """Agent-supplied ``scope='all-projects'`` selects the branch; the branch denies it.

    Parses the raw argument dict through the discriminated union exactly as the MCP wrapper
    does, then dispatches it. A project viewer must land in the all-projects gate — never in
    the granted-set rollup it *is* entitled to — and get back no rows.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _project_ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            request = TypeAdapter(AccountingReportRequest).validate_python(
                {"scope": "all-projects", "group_by": "principal"}
            )
            assert isinstance(request, AccountingAllProjectsReportRequest)
            resp = await report(pool, ctx, request=request)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert _rows(resp) == []
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_and_all_projects_audit_rows_stay_distinguishable(migrated_url: str) -> None:
    """Both scopes audit under one ``tool`` name; ``scope`` + ``platform_role`` separate them.

    The consolidation drops the per-scope tool name, so attribution has to survive in the
    remaining columns (ADR-0467): the granted-set read records the named target set and a
    null platform_role, the all-projects read records ``all-projects`` and the held roles.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            granted_ctx = _project_ctx(
                roles={"proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-a", "proj-b"),
            )
            assert (await report_granted_set(pool, granted_ctx)).status == "ok"
            auditor = _platform_ctx(PlatformRole.PLATFORM_AUDITOR)
            assert (await report_all_projects(pool, auditor)).status == "ok"
        rows = sorted(await _platform_audit_rows(migrated_url), key=lambda r: str(r[3]))
        assert [r[2] for r in rows] == ["accounting.report", "accounting.report"]
        assert rows[0][3] == "all-projects"
        assert rows[0][1] == "platform_auditor"
        assert rows[1][3] == "granted-set:proj-a,proj-b"
        assert rows[1][1] is None

    asyncio.run(_run())

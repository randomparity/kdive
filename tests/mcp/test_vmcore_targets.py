"""Tests for shared Run -> vmcore target resolution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools._vmcore_targets import (
    NO_BUILD,
    NO_DEBUGINFO,
    NO_VMCORE,
    RunVmcoreTarget,
    resolve_run_vmcore_target,
    vmcore_target_failure,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp._seed import seed_crashed_system, seed_run_on_system


def _ctx(
    role: Role | None = Role.VIEWER, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_vmcore_row(pool: AsyncConnectionPool, run_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('runs', %s, %s, 'e', 'sensitive', 'vmcore')",
            (run_id, f"local/runs/{run_id}/vmcore-host_dump"),
        )


async def _built_run_with_core(pool: AsyncConnectionPool) -> str:
    system_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(
        pool,
        system_id,
        debuginfo_ref="k/runs/r/vmlinux",
        build_id="deadbeef",
    )
    await _seed_vmcore_row(pool, run_id)
    return run_id


def test_resolve_run_vmcore_target_returns_port_inputs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            async with pool.connection() as conn:
                resolved = await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert isinstance(resolved, RunVmcoreTarget)
        assert resolved.debuginfo_ref == "k/runs/r/vmlinux"
        assert resolved.build_id == "deadbeef"
        assert resolved.vmcore_ref.endswith("/vmcore-host_dump")

    asyncio.run(_run())


def test_resolve_run_vmcore_target_rejects_bad_run_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_run_vmcore_target(conn, _ctx(), "not-a-uuid")

        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_resolve_run_vmcore_target_missing_build_id_is_not_found(migrated_url: str) -> None:
    # A run with a captured core + debuginfo but no recorded build step surfaces the no_build
    # precondition reason: not_found, not a malformed-input configuration_error (ADR-0097). The
    # vmcore row is seeded so the no_vmcore check (now first, ADR-0165) passes and the no_build
    # check is reached.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool,
                system_id,
                debuginfo_ref="k/runs/r/vmlinux",
                build_id=None,
            )
            await _seed_vmcore_row(pool, run_id)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert exc.value.details["reason"] == NO_BUILD

    asyncio.run(_run())


def test_resolve_run_vmcore_target_null_debuginfo_reason(migrated_url: str) -> None:
    # A run with a captured core but a null debuginfo_ref surfaces the no_debuginfo precondition
    # reason (#487). The vmcore row is seeded so the no_vmcore check (now first, ADR-0165) passes
    # and the debuginfo check is reached — this guards that the reorder keeps no_debuginfo distinct
    # for the core-present-but-unsymbolizable case.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, system_id, debuginfo_ref=None, build_id="deadbeef"
            )
            await _seed_vmcore_row(pool, run_id)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert exc.value.details["reason"] == NO_DEBUGINFO

    asyncio.run(_run())


def test_resolve_run_vmcore_target_booted_no_core_reason(migrated_url: str) -> None:
    # A built+booted run with no captured vmcore row surfaces the no_vmcore precondition reason
    # (#487). One of the two #553 acceptance cases.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, system_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
            )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert exc.value.details["reason"] == NO_VMCORE

    asyncio.run(_run())


def test_resolve_run_vmcore_target_console_crash_carries_kind(migrated_url: str) -> None:
    # A console_crash run with no captured core carries its declared kind on the no_vmcore
    # error's details, so the postmortem handler can redirect to the console (#734, ADR-0227).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool,
                system_id,
                debuginfo_ref="k/runs/r/vmlinux",
                build_id="deadbeef",
                expected_boot_failure={"kind": "console_crash", "pattern": "Kernel panic"},
            )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert exc.value.details["reason"] == NO_VMCORE
        assert exc.value.details["expected_boot_failure"] == "console_crash"

    asyncio.run(_run())


def test_resolve_run_vmcore_target_no_boot_failure_omits_kind(migrated_url: str) -> None:
    # A run without expected_boot_failure carries NO `expected_boot_failure` key on its no_vmcore
    # error — so the non-console-crash no_vmcore envelope stays byte-identical to today and
    # safe_error_details cannot leak a kind into its `data` (#734, ADR-0227).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, system_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
            )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.details["reason"] == NO_VMCORE
        assert "expected_boot_failure" not in exc.value.details

    asyncio.run(_run())


def test_resolve_run_vmcore_target_never_booted_reports_no_vmcore(migrated_url: str) -> None:
    # A never-booted run lacks debuginfo, build, AND a captured core at once. Triaging it through
    # the vmcore-centric resolver reports the operative gap (no_vmcore), not the earliest-unmet
    # build precondition (no_debuginfo). The other #553 acceptance case (ADR-0165).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert exc.value.details["reason"] == NO_VMCORE

    asyncio.run(_run())


def test_resolve_run_vmcore_target_absent_run_is_not_found(migrated_url: str) -> None:
    # The absent-Run / ungranted-project miss carries no reason token so the envelope cannot leak
    # membership (it must stay byte-identical to a genuinely-absent Run).
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_run_vmcore_target(conn, _ctx(), str(uuid4()))

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert "reason" not in exc.value.details

    asyncio.run(_run())


def test_vmcore_target_failure_maps_reason_to_next_actions() -> None:
    exc = CategorizedError("miss", category=ErrorCategory.NOT_FOUND, details={"reason": NO_VMCORE})
    resp = vmcore_target_failure("rid", exc)
    assert resp.error_category == "not_found"
    assert resp.data["reason"] == NO_VMCORE
    assert resp.suggested_next_actions == ["vmcore.fetch", "runs.get"]
    # detail stays the suppressed constant (no-leak seam, ADR-0123).
    assert resp.detail == "not found"


def test_vmcore_target_failure_no_reason_yields_no_next_actions() -> None:
    exc = CategorizedError("miss", category=ErrorCategory.NOT_FOUND)
    resp = vmcore_target_failure("rid", exc)
    assert resp.error_category == "not_found"
    assert "reason" not in resp.data
    assert resp.suggested_next_actions == []


def test_resolve_run_vmcore_target_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            async with pool.connection() as conn:
                with pytest.raises(AuthorizationError):
                    await resolve_run_vmcore_target(conn, _ctx(role=None), run_id)

    asyncio.run(_run())

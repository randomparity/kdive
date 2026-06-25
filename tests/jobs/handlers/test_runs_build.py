"""Coverage anchor for the split build run handler module."""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_hosts import get_by_name
from kdive.domain.capacity.state import SystemState
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import runs, runs_build
from kdive.jobs.payloads import BuildPayload
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system
from tests.mcp.systems_support import provider_resolver


def test_build_handler_is_exported_through_runs_facade() -> None:
    assert runs.build_handler is runs_build.build_handler
    assert runs._run_build is runs_build._run_build


def test_build_handler_registers_build_log_artifact_on_failure(
    migrated_url: str, monkeypatch: object, tmp_path: object
) -> None:
    """A failed build whose error carries a build-log key+etag registers a Run-owned artifact row.

    The failing job's failure context then surfaces the artifact id as
    ``failure_detail_build_log_artifact`` so ``runs.get`` can advertise ``refs["build-log"]``.
    """
    import pytest as _pytest
    from psycopg.rows import dict_row

    from kdive.domain.catalog.artifacts import Sensitivity
    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.providers.shared.build_host.publishing.build_log import (
        BUILD_LOG_ETAG_DETAIL,
        BUILD_LOG_KEY_DETAIL,
    )

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))

    class _FailingBuilder:
        def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
            raise CategorizedError(
                "make exited non-zero",
                category=ErrorCategory.BUILD_FAILURE,
                details={
                    BUILD_LOG_KEY_DETAIL: f"proj/runs/{run_id}/build-log",
                    BUILD_LOG_ETAG_DETAIL: "etag-build-log",
                },
            )

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            allocation_id = await seed_granted_allocation(pool)
            system_id = await seed_system(pool, allocation_id, SystemState.READY)
            run_id = await seed_running_run(pool, system_id)
            async with pool.connection() as conn:
                host = await get_by_name(conn, "worker-local")
            assert host is not None
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.BUILD,
                    BuildPayload(run_id=run_id, build_host_id=str(host.id)),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{run_id}:build",
                )
            try:
                async with pool.connection() as conn:
                    await runs.build_handler(
                        conn,
                        job,
                        resolver=provider_resolver(builder=_FailingBuilder()),
                        secret_registry=SecretRegistry(),
                    )
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.BUILD_FAILURE
            else:
                raise AssertionError("build_handler should have raised the build failure")

            object_key = f"proj/runs/{run_id}/build-log"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, owner_kind, sensitivity FROM artifacts WHERE object_key = %s",
                    (object_key,),
                )
                row = await cur.fetchone()
            assert row is not None
            assert row["owner_kind"] == "runs"
            assert row["sensitivity"] == Sensitivity.REDACTED.value

    asyncio.run(_run())


def test_build_handler_failure_without_build_log_registers_no_artifact(
    migrated_url: str, monkeypatch: object, tmp_path: object
) -> None:
    """A build failure carrying no build-log key registers no artifact row (unchanged path)."""
    import pytest as _pytest
    from psycopg.rows import dict_row

    from kdive.domain.errors import CategorizedError, ErrorCategory

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))

    class _BareFailingBuilder:
        def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
            raise CategorizedError("make exited non-zero", category=ErrorCategory.BUILD_FAILURE)

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            allocation_id = await seed_granted_allocation(pool)
            system_id = await seed_system(pool, allocation_id, SystemState.READY)
            run_id = await seed_running_run(pool, system_id)
            async with pool.connection() as conn:
                host = await get_by_name(conn, "worker-local")
            assert host is not None
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.BUILD,
                    BuildPayload(run_id=run_id, build_host_id=str(host.id)),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{run_id}:build",
                )
            try:
                async with pool.connection() as conn:
                    await runs.build_handler(
                        conn,
                        job,
                        resolver=provider_resolver(builder=_BareFailingBuilder()),
                        secret_registry=SecretRegistry(),
                    )
            except CategorizedError:
                pass
            else:
                raise AssertionError("build_handler should have raised the build failure")

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM artifacts WHERE owner_id = %s", (run_id,)
                )
                row = await cur.fetchone()
            assert row is not None
            assert row["n"] == 0

    asyncio.run(_run())
